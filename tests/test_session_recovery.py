import logging
import time
from unittest.mock import Mock

from cryptography.fernet import Fernet
import pytest

import app
import authentication
from config import Config
from http_client import HttpLidlClient
from lidl_client import UnexpectedHTTPStatusError
from session_store import SessionRenewalInterrupted, SessionStore, SessionTokens
from test_http_client import FakeResponse, FakeSession, allowance


def interrupted_store(tmp_path):
    store = SessionStore(tmp_path / "session.sqlite3", Fernet.generate_key().decode())
    store.save(SessionTokens("expired-access", "old-refresh", "test-client", "test-secret", 0))
    def rejected_refresh(tokens):
        raise UnexpectedHTTPStatusError(503)
    with pytest.raises(UnexpectedHTTPStatusError):
        store.access_token(rejected_refresh)
    return store


def test_interrupted_refresh_recovers_once_without_replaying_old_grant(tmp_path, monkeypatch):
    store = interrupted_store(tmp_path)
    clients = []
    def factory(config):
        client = HttpLidlClient(session_store=store)
        client._session.close()
        client._session = FakeSession([FakeResponse(data={"data": allowance(refill=0.80)})])
        clients.append(client)
        return client
    monkeypatch.setattr(app, "create_client", factory)
    monkeypatch.setenv("LIDL_USERNAME", "test-user")
    monkeypatch.setenv("LIDL_PASSWORD", "private-test-password")
    new = SessionTokens("new-access", "new-refresh", "test-client", "test-secret", time.time() + 3600)
    recovery = Mock(side_effect=lambda config, replace: store.bootstrap(lambda: new, replace=replace))
    monkeypatch.setattr(authentication, "bootstrap_from_environment", recovery)
    client = app.create_client_with_recovery(Config(client_mode="http"), logging.getLogger("test"))
    assert client.get_remaining_data() == 0.80
    recovery.assert_called_once()
    assert recovery.call_args.kwargs == {"replace": True}
    assert clients[0]._session.calls == []
    assert [call[1]["json"]["operationName"] for call in clients[1]._session.calls] == ["consumptions"]
    assert store.access_token(lambda _: pytest.fail("Renewed pair must be retained")) == "new-access"


def test_healthy_session_does_not_password_login(tmp_path, monkeypatch):
    store = SessionStore(tmp_path / "session.sqlite3", Fernet.generate_key().decode())
    store.save(SessionTokens("valid-access", "valid-refresh", "test-client", "test-secret", time.time() + 3600))
    client = HttpLidlClient(session_store=store)
    client._session.close()
    client._session = FakeSession([])
    monkeypatch.setattr(app, "create_client", lambda _: client)
    recovery = Mock()
    monkeypatch.setattr(authentication, "bootstrap_from_environment", recovery)
    assert app.create_client_with_recovery(Config(client_mode="http"), logging.getLogger("test")) is client
    recovery.assert_not_called()
    assert client._session.calls == []


def test_failed_recovery_is_single_attempt_and_never_logs_private_error(tmp_path, monkeypatch, capsys):
    store = interrupted_store(tmp_path)
    def factory(config):
        return HttpLidlClient(session_store=store)
    monkeypatch.setattr(app, "create_client", factory)
    monkeypatch.setenv("CLIENT_MODE", "http")
    monkeypatch.setenv("LIDL_USERNAME", "test-user")
    monkeypatch.setenv("LIDL_PASSWORD", "private-test-password")
    recovery = Mock(side_effect=RuntimeError("private-test-password response-body"))
    monkeypatch.setattr(authentication, "bootstrap_from_environment", recovery)
    pause = Mock()
    monkeypatch.setattr(app, "pause_after_failed_recovery", pause)
    assert app.main() == 1
    recovery.assert_called_once()
    pause.assert_called_once()
    output = capsys.readouterr().err
    assert "Normal sign-in recovery failed (RuntimeError)" in output
    assert "private-test-password" not in output
    assert "response-body" not in output


def test_fresh_503_still_preserves_unknown_refresh_for_the_next_check(tmp_path, monkeypatch):
    store = SessionStore(tmp_path / "session.sqlite3", Fernet.generate_key().decode())
    store.save(SessionTokens("expired-access", "old-refresh", "test-client", "test-secret", 0))
    client = HttpLidlClient(session_store=store)
    client._session.close()
    client._session = FakeSession([FakeResponse(status=503)])
    monkeypatch.setattr(app, "create_client", lambda _: client)
    recovery = Mock()
    monkeypatch.setattr(authentication, "bootstrap_from_environment", recovery)
    with pytest.raises(UnexpectedHTTPStatusError):
        app.create_client_with_recovery(Config(client_mode="http"), logging.getLogger("test"))
    recovery.assert_not_called()
    assert len(client._session.calls) == 1
    with pytest.raises(SessionRenewalInterrupted):
        store.access_token(lambda _: pytest.fail("Ambiguous old refresh must not be replayed"))


def test_manual_recovery_failure_does_not_pause_a_cloud_timer(monkeypatch):
    monkeypatch.setenv("CLOUD_SCHEDULED_CHECK", "false")
    database = Mock()
    monkeypatch.setattr("postgres_store._Postgres", database)
    app.pause_after_failed_recovery(Config(state_backend="postgres"), logging.getLogger("test"))
    database.assert_not_called()


def test_failed_automatic_recovery_commits_a_timer_pause(monkeypatch):
    monkeypatch.setenv("CLOUD_SCHEDULED_CHECK", "true")
    connection = Mock()
    connection.transaction.return_value.__enter__ = Mock()
    connection.transaction.return_value.__exit__ = Mock()
    store = Mock()
    store._database.return_value.__enter__ = Mock(return_value=connection)
    store._database.return_value.__exit__ = Mock()
    monkeypatch.setattr("postgres_store._Postgres", Mock(return_value=store))
    pause = Mock()
    monkeypatch.setattr("cloud_scheduler.pause", pause)
    app.pause_after_failed_recovery(Config(state_backend="postgres"), logging.getLogger("test"))
    pause.assert_called_once_with(connection)
    connection.transaction.return_value.__exit__.assert_called_once_with(None, None, None)
