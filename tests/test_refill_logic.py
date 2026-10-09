from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import logging
from threading import Barrier

import pytest

from config import Config
from lidl_client import (
    AuthenticationExpiredError, ClientNotConfiguredError, MockLidlClient,
    RequestTimeoutError, ServiceUnavailableError, UnexpectedResponseError, create_client,
    UnexpectedHTTPStatusError,
)
from refill_logic import run_once
from state_store import SQLiteStateStore


@pytest.fixture
def config(tmp_path):
    return Config(dry_run=False, state_db_path=tmp_path / "state.sqlite3")


@pytest.fixture
def state(config):
    return SQLiteStateStore(config.state_db_path)


def check(client, config, state, now=1000, waits=None):
    return run_once(client, config, state, clock=lambda: now,
                    sleep=(lambda seconds: waits.append(seconds)) if waits is not None else lambda _: None)


@pytest.mark.parametrize(("remaining", "expected_calls"), [(0.31, 0), (0.30, 1), (0.20, 1), (1.30, 0)])
def test_threshold(remaining, expected_calls, config, state):
    client = MockLidlClient(remaining)
    result = check(client, config, state)
    assert client.refill_calls == expected_calls
    assert result.status == ("confirmed" if expected_calls else "above_threshold")
    assert result.exit_code == 0


def test_dry_run_does_not_refill_or_record_intent(config, state):
    client = MockLidlClient(0.20)
    assert check(client, replace(config, dry_run=True), state).status == "would_refill"
    assert client.refill_calls == 0
    assert state.get() is None


def test_delayed_update_polls_reads_without_repeating_refill(config, state):
    client = MockLidlClient(0.20, update_after_reads=2)
    waits = []
    result = check(client, config, state, waits=waits)
    assert result.status == "confirmed"
    assert result.after_gb == pytest.approx(1.20)
    assert client.refill_calls == 1
    assert waits == [12, 5, 5]
    assert state.get().status == "confirmed"


def test_failed_verification_remains_blocked_across_restart_and_cooldown(config, state):
    client = MockLidlClient(0.20, outcome="no_change")
    assert check(client, config, state).status == "unconfirmed"
    assert state.get().status == "pending"
    reopened = SQLiteStateStore(config.state_db_path)
    for now in [1001, 1599, 1600, 10000]:
        assert check(client, config, reopened, now=now).status == "pending"
    assert client.refill_calls == 1


def test_later_increase_resolves_pending_without_another_refill(config, state):
    client = MockLidlClient(0.20, update_after_reads=3)
    assert check(client, config, state).status == "unconfirmed"
    assert check(client, config, state, now=1300).status == "above_threshold"
    assert state.get().status == "confirmed"
    assert client.refill_calls == 1


def test_dry_run_keeps_pending_state_unchanged_even_if_increase_is_seen(config, state):
    state.claim(0.20, 1000, 600)
    client = MockLidlClient(1.20)
    assert check(client, replace(config, dry_run=True), state, now=1300).status == "above_threshold"
    assert state.get().status == "pending"
    assert client.refill_calls == 0


def test_success_still_enforces_full_ten_minute_cooldown(config, state):
    client = MockLidlClient(0.20)
    assert check(client, config, state).status == "confirmed"
    client.remaining_gb = 0.20
    assert check(client, config, state, now=1599).status == "cooldown"
    assert client.refill_calls == 1
    assert check(client, config, state, now=1600).status == "confirmed"
    assert client.refill_calls == 2


@pytest.mark.parametrize("outcome", ["timeout", "rejected"])
def test_refill_error_keeps_pending_and_never_retries(outcome, config, state):
    client = MockLidlClient(0.20, outcome=outcome, update_after_reads=100)
    result = check(client, config, state)
    assert result.status == "request_uncertain"
    assert result.exit_code == 2
    assert check(client, config, state, now=10000).status == "pending"
    assert client.refill_calls == 1


def test_intent_is_committed_before_mutation_and_survives_crash(config, state):
    class CrashingClient(MockLidlClient):
        def activate_refill(self):
            assert SQLiteStateStore(config.state_db_path).get().status == "pending"
            self.refill_calls += 1
            raise SystemExit("simulated process interruption")

    client = CrashingClient(0.20)
    with pytest.raises(SystemExit):
        check(client, config, state)
    assert check(client, config, SQLiteStateStore(config.state_db_path), now=10000).status == "pending"
    assert client.refill_calls == 1


@pytest.mark.parametrize("error_type", [AuthenticationExpiredError, RequestTimeoutError, ServiceUnavailableError, UnexpectedResponseError])
def test_initial_read_failure_requests_nothing(error_type, config, state):
    class FailingClient(MockLidlClient):
        def get_remaining_data(self):
            raise error_type("do not log account response")

    client = FailingClient(0.20)
    with pytest.raises(error_type):
        check(client, config, state)
    assert client.refill_calls == 0
    assert state.get() is None


def test_verification_failure_keeps_intent_and_does_not_log_response(config, state, caplog):
    class FailingVerificationClient(MockLidlClient):
        def get_remaining_data(self):
            if self.refill_calls:
                raise AuthenticationExpiredError("private-token-and-account-details")
            return super().get_remaining_data()

    client = FailingVerificationClient(0.20)
    with caplog.at_level(logging.INFO):
        assert check(client, config, state).status == "unconfirmed"
    assert state.get().status == "pending"
    assert client.refill_calls == 1
    assert "private-token-and-account-details" not in caplog.text


@pytest.mark.parametrize("remaining", [None, True, "0.20 GB", float("nan"), float("inf"), -1])
def test_bad_client_data_fails_closed(remaining, config, state):
    client = MockLidlClient(remaining)
    with pytest.raises(UnexpectedResponseError):
        check(client, config, state)
    assert client.refill_calls == 0
    assert state.get() is None


def test_overlapping_runs_send_only_one_refill(config, state):
    barrier = Barrier(2)

    class OverlappingClient(MockLidlClient):
        def get_remaining_data(self):
            if self.refill_calls == 0:
                barrier.wait(timeout=10)
            return super().get_remaining_data()

    clients = [OverlappingClient(0.20), OverlappingClient(0.20)]
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda client: check(client, config, SQLiteStateStore(config.state_db_path)), clients))
    assert sum(client.refill_calls for client in clients) == 1
    assert sorted(result.status for result in results) in [["confirmed", "cooldown"], ["confirmed", "pending"]]


def test_http_mode_requires_an_access_token(config):
    with pytest.raises(ClientNotConfiguredError, match="LIDL_ACCESS_TOKEN"):
        create_client(replace(config, client_mode="http"))


def test_error_response_after_applied_refill_is_confirmed_without_retry(config, state, caplog):
    class AppliedRefillWithError(MockLidlClient):
        def activate_refill(self):
            super().activate_refill()
            raise UnexpectedHTTPStatusError(500)

    client = AppliedRefillWithError(0.20)
    with caplog.at_level(logging.INFO):
        result = check(client, config, state)
    assert result.status == "confirmed"
    assert result.after_gb == pytest.approx(1.20)
    assert state.get().status == "confirmed"
    assert client.refill_calls == 1
    assert "HTTP 500" in caplog.text


def test_timeout_after_applied_refill_is_confirmed_without_retry(config, state):
    client = MockLidlClient(0.20, outcome="timeout")
    assert check(client, config, state).status == "confirmed"
    assert client.refill_calls == 1
