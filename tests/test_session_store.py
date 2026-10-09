from dataclasses import replace
import multiprocessing
import os
from pathlib import Path
import sqlite3
import time

from cryptography.fernet import Fernet
import pytest

from session_store import SessionStateError, SessionStore, SessionTokens


def tokens(expiry=5000):
    return SessionTokens("private-access", "private-refresh", "private-client", "private-secret", expiry)


def test_encrypted_session_survives_restart_without_plaintext_tokens(tmp_path):
    key = Fernet.generate_key().decode()
    path = tmp_path / "session.sqlite3"
    SessionStore(path, key, clock=lambda: 1000).save(tokens())
    for secret in ("private-access", "private-refresh", "private-client", "private-secret"):
        assert secret.encode() not in path.read_bytes()
        assert secret not in repr(tokens())
    restored = SessionStore(path, key, clock=lambda: 1000)
    assert restored.access_token(lambda _: pytest.fail("Valid tokens must not be renewed")) == "private-access"


def test_expired_tokens_are_rotated_and_new_pair_survives_restart(tmp_path):
    key = Fernet.generate_key().decode()
    path = tmp_path / "session.sqlite3"
    store = SessionStore(path, key, clock=lambda: 1000)
    store.save(tokens(1000))
    calls = []

    def renew(old):
        calls.append(old)
        return replace(old, access_token="new-access", refresh_token="new-refresh", expires_at=5000)

    assert store.access_token(renew) == "new-access"
    restarted = SessionStore(path, key, clock=lambda: 1000)
    assert restarted.access_token(renew) == "new-access"
    assert len(calls) == 1
    # A later forced renewal must use the newest refresh token.
    restarted.access_token(renew, rejected_token="new-access")
    assert calls[1].refresh_token == "new-refresh"


def test_another_process_already_renewed_a_rejected_access_token(tmp_path):
    store = SessionStore(tmp_path / "session.sqlite3", Fernet.generate_key().decode(), clock=lambda: 1000)
    store.save(tokens())
    assert store.access_token(lambda _: pytest.fail("Another process already renewed"), rejected_token="older-access") == "private-access"


@pytest.mark.parametrize("failure", [RuntimeError("private-server-content"), KeyboardInterrupt()])
def test_interrupted_renewal_stays_pending_and_is_never_retried(tmp_path, failure):
    key = Fernet.generate_key().decode()
    path = tmp_path / "session.sqlite3"
    store = SessionStore(path, key, clock=lambda: 1000)
    store.save(tokens(1000))

    def fail(_):
        raise failure

    with pytest.raises(type(failure)):
        store.access_token(fail)
    restarted = SessionStore(path, key, clock=lambda: 1000)
    with pytest.raises(SessionStateError, match="interrupted"):
        restarted.access_token(lambda _: pytest.fail("Uncertain renewal must not be retried"))
    restarted.save(tokens(), replace=True)
    assert restarted.access_token(fail) == "private-access"


def test_wrong_key_or_tampered_ciphertext_stops_before_network(tmp_path):
    key = Fernet.generate_key().decode()
    path = tmp_path / "session.sqlite3"
    SessionStore(path, key).save(tokens())
    wrong_key = SessionStore(path, Fernet.generate_key().decode())
    with pytest.raises(SessionStateError, match="decrypt"):
        wrong_key.access_token(lambda _: pytest.fail("Must not send invalid tokens"))
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE session SET ciphertext = ?", (b"tampered",))
    with pytest.raises(SessionStateError, match="decrypt"):
        SessionStore(path, key).access_token(lambda _: pytest.fail("Must not send invalid tokens"))


def test_missing_session_and_accidental_replacement_fail_closed(tmp_path):
    store = SessionStore(tmp_path / "session.sqlite3", Fernet.generate_key().decode())
    with pytest.raises(SessionStateError, match="bootstrap"):
        store.access_token(lambda _: pytest.fail("Missing session must not call network"))
    store.save(tokens())
    with pytest.raises(SessionStateError, match="already exists"):
        store.save(tokens())


def test_bootstrap_refuses_existing_session_before_attempting_password_login(tmp_path):
    store = SessionStore(tmp_path / "session.sqlite3", Fernet.generate_key().decode(), clock=lambda: 1000)
    store.bootstrap(lambda: tokens())
    with pytest.raises(SessionStateError, match="already exists"):
        store.bootstrap(lambda: pytest.fail("Existing session must not trigger another login"))


def test_interrupted_replacement_login_does_not_resume_old_tokens(tmp_path):
    store = SessionStore(tmp_path / "session.sqlite3", Fernet.generate_key().decode(), clock=lambda: 1000)
    store.save(tokens())

    def fail():
        raise RuntimeError("Login response lost")

    with pytest.raises(RuntimeError):
        store.bootstrap(fail, replace=True)
    with pytest.raises(SessionStateError, match="interrupted"):
        store.access_token(lambda _: pytest.fail("Old tokens may have been invalidated"))
    store.bootstrap(lambda: replace(tokens(), access_token="replacement-access"), replace=True)
    assert store.access_token(lambda _: pytest.fail("Replacement tokens must be retained")) == "replacement-access"


def _renew_from_process(path, key, start, calls, results):
    store = SessionStore(Path(path), key, clock=lambda: 1000, lock_timeout=5)
    if not start.wait(timeout=15):
        raise RuntimeError("Concurrent test did not start.")

    def renew(old):
        calls.put("renewed")
        time.sleep(0.15)
        return replace(old, access_token="new-access", refresh_token="new-refresh", expires_at=5000)

    token = store.access_token(renew)
    results.put(token)


def test_separate_processes_cannot_rotate_the_same_refresh_token(tmp_path):
    path = tmp_path / "session.sqlite3"
    key = Fernet.generate_key().decode()
    SessionStore(path, key).save(tokens(1000))
    context = multiprocessing.get_context("spawn")
    start, calls, results = context.Event(), context.Queue(), context.Queue()
    processes = [context.Process(target=_renew_from_process, args=(str(path), key, start, calls, results)) for _ in range(3)]
    try:
        for process in processes:
            process.start()
        start.set()
        outcomes = [results.get(timeout=20) for _ in processes]
        for process in processes:
            process.join(timeout=20)
            assert process.exitcode == 0
        assert outcomes == ["new-access"] * 3
        assert calls.get(timeout=5) == "renewed"
        assert calls.empty()
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
        calls.close()
        results.close()


def _crash_during_renewal(path, key):
    store = SessionStore(Path(path), key, clock=lambda: 1000)
    store.access_token(lambda _: os._exit(7))


def test_process_crash_preserves_committed_renewal_marker(tmp_path):
    path = tmp_path / "session.sqlite3"
    key = Fernet.generate_key().decode()
    SessionStore(path, key).save(tokens(1000))
    process = multiprocessing.get_context("spawn").Process(target=_crash_during_renewal, args=(str(path), key))
    process.start()
    process.join(timeout=20)
    try:
        assert process.exitcode == 7
        with pytest.raises(SessionStateError, match="interrupted"):
            SessionStore(path, key).access_token(lambda _: pytest.fail("Must not replay renewal after a crash"))
    finally:
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
