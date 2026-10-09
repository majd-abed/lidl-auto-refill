from datetime import datetime, timezone
import json
from unittest.mock import Mock

import pytest

import cloud_scheduler
from config import ConfigurationError


@pytest.mark.parametrize("repository", ["owner/repo/other", "owner/repo?token=secret", "https://github.com/owner/repo"])
def test_dispatch_rejects_targets_that_could_change_the_http_endpoint(repository):
    with pytest.raises(ConfigurationError):
        cloud_scheduler.validate_dispatch_settings("github_pat_test_only", repository)


def test_enable_missing_scoped_token_does_not_open_the_database(monkeypatch, capsys):
    monkeypatch.setenv("CLIENT_MODE", "http")
    monkeypatch.setenv("STATE_BACKEND", "postgres")
    monkeypatch.setenv("DATABASE_URL", "postgresql://example:private@localhost/postgres")
    monkeypatch.delenv("GITHUB_DISPATCH_TOKEN", raising=False)
    monkeypatch.setattr("sys.argv", ["cloud_scheduler.py", "enable"])
    database = Mock()
    monkeypatch.setattr(cloud_scheduler, "_Postgres", database)
    assert cloud_scheduler.main() == 1
    database.assert_not_called()
    assert "GITHUB_DISPATCH_TOKEN" in capsys.readouterr().out


def test_status_excludes_response_body_and_authentication_data():
    connection = Mock()
    job, ticks, requests = Mock(), Mock(), Mock()
    connection.execute.side_effect = [job, ticks, requests]
    job.fetchone.return_value = (True, "*/5 * * * *")
    now = datetime(2026, 10, 10, tzinfo=timezone.utc)
    ticks.fetchall.return_value = [(now, "succeeded")]
    requests.fetchall.return_value = [(123, now, 200, False, False, json.dumps({"workflow_run_id": 456, "Authorization": "Bearer private-value", "html_url": "https://example.com/private"}))]
    report = cloud_scheduler.status(connection)
    encoded = json.dumps(report)
    assert report["dispatches"][0]["workflow_run_id"] == 456
    assert "private" not in encoded
    assert "Authorization" not in encoded


def test_preparing_an_existing_timer_does_not_change_activation():
    connection = Mock()
    connection.execute.return_value.fetchone.return_value = (123,)
    cloud_scheduler.prepare(connection)
    queries = [call.args[0] for call in connection.execute.call_args_list]
    assert not any("cron.alter_job" in query or "cron.schedule(" in query for query in queries)


def test_preparing_a_new_timer_explicitly_leaves_it_inactive():
    connection = Mock()
    sql, lookup, scheduled, paused = Mock(), Mock(), Mock(), Mock()
    connection.execute.side_effect = [sql, lookup, scheduled, paused]
    lookup.fetchone.return_value = None
    scheduled.fetchone.return_value = (123,)
    cloud_scheduler.prepare(connection)
    assert connection.execute.call_args.args == ("SELECT cron.alter_job(%s, active := false)", (123,))
