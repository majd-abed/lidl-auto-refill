from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import multiprocessing
import os
import time
from uuid import uuid4

from cryptography.fernet import Fernet
import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict
import pytest

from config import Config
from lidl_client import MockLidlClient
from postgres_store import PostgresSessionStore, PostgresStateStore
from refill_logic import run_once
from session_store import SessionStateError, SessionTokens
from state_store import StateStoreError


@pytest.fixture
def database():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("Real PostgreSQL integration tests need TEST_DATABASE_URL (provided by CI).")
    if conninfo_to_dict(url).get("host") not in {"localhost", "127.0.0.1", "::1"}:
        pytest.fail("Integration tests use a local disposable PostgreSQL service only.")
    account = "test_" + uuid4().hex
    PostgresStateStore(url, account)
    yield url, account
    with psycopg.connect(url, autocommit=True) as connection:
        for table in ("refills", "sessions"):
            connection.execute(sql.SQL("DELETE FROM lidl_automation.{} WHERE account_key = %s").format(sql.Identifier(table)), (account,))


def session_tokens(expiry=5000):
    return SessionTokens("private-access", "private-refresh", "private-client", "private-secret", expiry)


def _claim_from_process(url, account, start, results):
    store = PostgresStateStore(url, account)
    if not start.wait(timeout=15):
        raise RuntimeError("Concurrent test did not start.")
    result = store.claim(0.20, 1000, 600)
    results.put("claimed" if result.record else result.blocked_by)


def test_postgres_separate_processes_claim_one_refill(database):
    url, account = database
    context = multiprocessing.get_context("spawn")
    start, results = context.Event(), context.Queue()
    processes = [context.Process(target=_claim_from_process, args=(url, account, start, results)) for _ in range(3)]
    try:
        for process in processes:
            process.start()
        start.set()
        outcomes = [results.get(timeout=20) for _ in processes]
        for process in processes:
            process.join(timeout=20)
            assert process.exitcode == 0
        assert outcomes.count("claimed") == 1
        assert outcomes.count("pending") == 2
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
        results.close()


def test_postgres_pending_state_survives_restart_and_expired_cooldown(database):
    url, account = database
    store = PostgresStateStore(url, account)
    record = store.claim(0.20, 1000, 600).record
    restored = PostgresStateStore(url, account)
    assert restored.get().request_id == record.request_id
    assert restored.claim(0.20, 100000, 600).blocked_by == "pending"


def test_postgres_confirmation_and_cooldown_cannot_clear_a_new_request(database):
    url, account = database
    store = PostgresStateStore(url, account)
    first = store.claim(0.20, 1000, 600).record
    store.confirm(first.request_id)
    store.confirm(first.request_id)
    assert store.claim(0.20, 1599, 600).blocked_by == "cooldown"
    second = store.claim(0.20, 1600, 600).record
    with pytest.raises(StateStoreError):
        store.confirm(first.request_id)
    assert store.get().request_id == second.request_id


def test_postgres_intent_is_committed_before_mutation_and_survives_crash(database):
    url, account = database
    store = PostgresStateStore(url, account)

    class CrashingClient(MockLidlClient):
        def activate_refill(self):
            assert PostgresStateStore(url, account).get().status == "pending"
            self.refill_calls += 1
            raise SystemExit("Simulated runner loss")

    client = CrashingClient(0.20)
    config = Config(dry_run=False)
    with pytest.raises(SystemExit):
        run_once(client, config, store, clock=lambda: 1000, sleep=lambda _: None)
    result = run_once(client, config, PostgresStateStore(url, account), clock=lambda: 100000, sleep=lambda _: None)
    assert result.status == "pending"
    assert client.refill_calls == 1


def _renew_from_process(url, account, key, start, calls, results):
    store = PostgresSessionStore(url, key, account, clock=lambda: 1000, lock_timeout=5)
    if not start.wait(timeout=15):
        raise RuntimeError("Concurrent test did not start.")

    def renew(tokens):
        calls.put("renewed")
        time.sleep(0.2)
        return replace(tokens, access_token="new-access", refresh_token="new-refresh", expires_at=5000)

    results.put(store.access_token(renew))


def test_postgres_separate_processes_rotate_one_token_pair(database):
    url, account = database
    key = Fernet.generate_key().decode()
    PostgresSessionStore(url, key, account, clock=lambda: 1000).save(session_tokens())
    context = multiprocessing.get_context("spawn")
    start, calls, results = context.Event(), context.Queue(), context.Queue()
    # Force renewal of this initially valid pair before starting workers.
    with psycopg.connect(url, autocommit=True) as connection:
        from session_store import TokenCipher
        connection.execute("UPDATE lidl_automation.sessions SET ciphertext = %s WHERE account_key = %s", (TokenCipher(key).encrypt(session_tokens(1000)), account))
    processes = [context.Process(target=_renew_from_process, args=(url, account, key, start, calls, results)) for _ in range(3)]
    try:
        for process in processes:
            process.start()
        start.set()
        assert [results.get(timeout=20) for _ in processes] == ["new-access"] * 3
        for process in processes:
            process.join(timeout=20)
            assert process.exitcode == 0
        assert calls.get(timeout=5) == "renewed"
        assert calls.empty()
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
        calls.close()
        results.close()


def test_postgres_rotated_pair_is_encrypted_and_reused_after_restart(database):
    url, account = database
    key = Fernet.generate_key().decode()
    store = PostgresSessionStore(url, key, account, clock=lambda: 1000)
    store.save(session_tokens())
    store.access_token(lambda old: replace(old, access_token="new-access", refresh_token="new-refresh"), rejected_token="private-access")
    assert PostgresSessionStore(url, key, account, clock=lambda: 1000).access_token(lambda _: pytest.fail("Persisted token must be reused")) == "new-access"
    with psycopg.connect(url) as connection:
        ciphertext = connection.execute("SELECT ciphertext FROM lidl_automation.sessions WHERE account_key = %s", (account,)).fetchone()[0]
    for secret in (b"new-access", b"new-refresh", b"private-client", b"private-secret"):
        assert secret not in ciphertext


def _crash_during_rotation(url, account, key):
    store = PostgresSessionStore(url, key, account, clock=lambda: 1000)
    store.access_token(lambda _: os._exit(7), rejected_token="private-access")


def test_postgres_runner_loss_keeps_rotation_marker_and_never_replays(database):
    url, account = database
    key = Fernet.generate_key().decode()
    PostgresSessionStore(url, key, account, clock=lambda: 1000).save(session_tokens())
    process = multiprocessing.get_context("spawn").Process(target=_crash_during_rotation, args=(url, account, key))
    process.start()
    process.join(timeout=20)
    try:
        assert process.exitcode == 7
        restored = PostgresSessionStore(url, key, account, clock=lambda: 1100, lock_timeout=1)
        with pytest.raises(SessionStateError, match="interrupted"):
            restored.access_token(lambda _: pytest.fail("Lost refresh response must not be replayed"))
        restored.bootstrap(lambda: session_tokens(), replace=True)
        assert restored.access_token(lambda _: pytest.fail("New login must be reused")) == "private-access"
    finally:
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)


def test_postgres_wrong_key_stops_before_renewal(database):
    url, account = database
    PostgresSessionStore(url, Fernet.generate_key().decode(), account, clock=lambda: 1000).save(session_tokens())
    other = PostgresSessionStore(url, Fernet.generate_key().decode(), account, clock=lambda: 1000)
    with pytest.raises(SessionStateError, match="decrypt"):
        other.access_token(lambda _: pytest.fail("Wrong key must not initiate authentication"))


def test_postgres_accounts_do_not_share_state(database):
    url, account = database
    PostgresStateStore(url, account).claim(0.20, 1000, 600)
    assert PostgresStateStore(url, account + "_other").get() is None


def test_postgres_public_api_role_cannot_read_or_modify_state(database):
    url, account = database
    PostgresStateStore(url, account).claim(0.20, 1000, 600)
    role = "api_test_" + uuid4().hex
    with psycopg.connect(url, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE ROLE {}").format(sql.Identifier(role)))
        try:
            connection.execute(sql.SQL("GRANT USAGE ON SCHEMA lidl_automation TO {}").format(sql.Identifier(role)))
            connection.execute(sql.SQL("GRANT SELECT, UPDATE ON lidl_automation.refills, lidl_automation.sessions TO {}").format(sql.Identifier(role)))
            with connection.transaction():
                connection.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(role)))
                assert connection.execute("SELECT * FROM lidl_automation.refills WHERE account_key = %s", (account,)).fetchall() == []
                assert connection.execute("UPDATE lidl_automation.refills SET status = 'confirmed' WHERE account_key = %s", (account,)).rowcount == 0
        finally:
            connection.execute(sql.SQL("REVOKE ALL ON lidl_automation.refills, lidl_automation.sessions FROM {}").format(sql.Identifier(role)))
            connection.execute(sql.SQL("REVOKE ALL ON SCHEMA lidl_automation FROM {}").format(sql.Identifier(role)))
            connection.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))


def test_postgres_connection_errors_never_echo_secrets(monkeypatch):
    def fail(*args, **kwargs):
        assert kwargs["sslmode"] == "verify-full"
        assert kwargs["prepare_threshold"] is None
        raise psycopg.OperationalError("private-database-password")

    monkeypatch.setattr(psycopg, "connect", fail)
    with pytest.raises(StateStoreError) as caught:
        PostgresStateStore("postgresql://test:private-database-password@database.example/test?sslmode=disable")
    assert "private-database-password" not in str(caught.value)
