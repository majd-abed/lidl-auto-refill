from unittest.mock import Mock

import pytest

import app
from config import Config
from lidl_client import AllowancePeriodExpiredError, LidlClient, UnexpectedResponseError
from state_store import SQLiteStateStore


def prepare_run(monkeypatch, tmp_path, reads):
    config = Config(client_mode="http", dry_run=False, state_db_path=tmp_path / "refills.sqlite3", verification_initial_delay=0, verification_poll_interval=0)
    store = SQLiteStateStore(config.state_db_path)
    client = Mock(spec=LidlClient)
    client.get_remaining_data.side_effect = reads
    monkeypatch.setattr(app.Config, "from_env", lambda: config)
    monkeypatch.setattr(app, "create_client_with_recovery", lambda config, logger: client)
    monkeypatch.setattr(app, "create_state_store", lambda config: store)
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    return client, store, summary


def test_rollover_waits_without_claiming_refill_or_reporting_a_current_balance(monkeypatch, tmp_path, capsys):
    client, store, summary = prepare_run(monkeypatch, tmp_path, [AllowancePeriodExpiredError("Expired period")])
    assert app.main() == 0
    client.activate_refill.assert_not_called()
    assert store.get() is None
    client.close.assert_called_once()
    output = capsys.readouterr().err
    assert "waiting for a current allowance snapshot" in output
    assert "Remaining allowance:" not in output
    assert "five-minute check" in summary.read_text()


def test_waiting_period_does_not_hide_or_clear_an_unverified_refill(monkeypatch, tmp_path):
    client, store, summary = prepare_run(monkeypatch, tmp_path, [AllowancePeriodExpiredError("Expired period")])
    previous = store.claim(0.20, 1000, 600).record
    assert app.main() == 2
    client.activate_refill.assert_not_called()
    assert store.get() == previous
    assert "still unverified" in summary.read_text()


def test_new_period_automatically_resumes_regular_checks(monkeypatch, tmp_path):
    client, store, summary = prepare_run(monkeypatch, tmp_path, [AllowancePeriodExpiredError("Expired period"), 24.22])
    assert app.main() == 0
    summary.unlink()
    assert app.main() == 0
    client.activate_refill.assert_not_called()
    assert store.get() is None
    assert not summary.exists()


def test_expired_verification_after_a_mutation_keeps_the_claim_and_error(monkeypatch, tmp_path):
    client, store, summary = prepare_run(monkeypatch, tmp_path, [0.20, AllowancePeriodExpiredError("Expired period")])
    assert app.main() == 2
    client.activate_refill.assert_called_once()
    assert store.get().status == "pending"
    assert not summary.exists()


@pytest.mark.parametrize("message", ["Invalid numeric allowance", "Missing data", "Unknown response format"])
def test_other_response_errors_still_fail_and_do_not_become_waiting(monkeypatch, tmp_path, message):
    client, store, summary = prepare_run(monkeypatch, tmp_path, [UnexpectedResponseError(message)])
    assert app.main() == 1
    client.activate_refill.assert_not_called()
    assert store.get() is None
    assert not summary.exists()
