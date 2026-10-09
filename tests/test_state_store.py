import multiprocessing
from pathlib import Path
import sqlite3

import pytest

from state_store import SQLiteStateStore, StateStoreError


def _claim_from_process(path, start, results):
    store = SQLiteStateStore(Path(path))
    if not start.wait(timeout=15):
        raise RuntimeError("Concurrent test did not start.")
    result = store.claim(0.20, 1000, 600)
    results.put("claimed" if result.record is not None else result.blocked_by)


def test_separate_processes_cannot_claim_the_same_refill(tmp_path):
    path = tmp_path / "state.sqlite3"
    SQLiteStateStore(path)
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    results = context.Queue()
    processes = [context.Process(target=_claim_from_process, args=(str(path), start, results)) for _ in range(3)]
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


def test_corrupt_database_fails_closed(tmp_path):
    path = tmp_path / "state.sqlite3"
    path.write_text("invalid database", encoding="utf-8")
    with pytest.raises(StateStoreError):
        SQLiteStateStore(path)


def test_invalid_stored_allowance_fails_closed(tmp_path):
    store = SQLiteStateStore(tmp_path / "state.sqlite3")
    store.claim(0.20, 1000, 600)
    with sqlite3.connect(store.path) as connection:
        connection.execute("UPDATE refill_state SET before_gb = -1 WHERE id = 1")
    with pytest.raises(StateStoreError):
        store.get()


def test_old_confirmation_cannot_clear_new_request(tmp_path):
    store = SQLiteStateStore(tmp_path / "state.sqlite3")
    first = store.claim(0.20, 1000, 600).record
    store.confirm(first.request_id)
    second = store.claim(0.20, 1600, 600).record
    with pytest.raises(StateStoreError):
        store.confirm(first.request_id)
    assert store.get().request_id == second.request_id
    assert store.get().status == "pending"


def test_two_observers_can_confirm_the_same_request(tmp_path):
    store = SQLiteStateStore(tmp_path / "state.sqlite3")
    record = store.claim(0.20, 1000, 600).record
    other = SQLiteStateStore(store.path)
    store.confirm(record.request_id)
    other.confirm(record.request_id)
    assert store.get().status == "confirmed"
    assert store.get().requested_at == 1000


def test_clock_moving_backwards_does_not_bypass_cooldown(tmp_path):
    store = SQLiteStateStore(tmp_path / "state.sqlite3")
    record = store.claim(0.20, 1000, 600).record
    store.confirm(record.request_id)
    assert store.claim(0.20, 900, 600).blocked_by == "cooldown"
