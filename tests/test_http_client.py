from copy import deepcopy
from datetime import datetime, timezone
import logging

import pytest
import requests

from config import Config
from http_client import FREE_REFILL_OPTION_ID, HttpLidlClient, parse_api_allowance
from lidl_client import (
    AuthenticationExpiredError, RefillRequestError, RequestTimeoutError,
    ServiceUnavailableError, UnexpectedHTTPStatusError, UnexpectedResponseError,
)
from refill_logic import run_once
from state_store import SQLiteStateStore


def allowance(base=0.0, refill=0.20):
    def row(kind, left, maximum):
        return {
            "type": kind, "left": left, "max": maximum, "unit": "GB",
            "expirationDate": "2099-01-01T00:00:00+00:00",
            "tariffOrOptions": [{"consumptions": [{"type": kind, "left": left, "unit": "GB"}]}],
        }
    return {"consumptions": {"consumptionsForUnit": [row("DATA", base, 25), row("REFILLABLE_DATA", refill, 1)]}}


class FakeResponse:
    def __init__(self, status=200, data=None, invalid_json=False):
        self.status_code = status
        self.data = data
        self.invalid_json = invalid_json
        self.closed = False

    def json(self):
        if self.invalid_json:
            raise ValueError("private-response-content")
        return self.data

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        reply = next(self.replies)
        if isinstance(reply, Exception):
            raise reply
        return reply

    def close(self):
        pass


def http_client(replies):
    client = HttpLidlClient("test-placeholder-token")
    client._session.close()
    client._session = FakeSession(replies)
    return client


def test_aggregate_base_and_refill_without_nested_double_counting():
    assert parse_api_allowance(allowance(base=0.10, refill=0.20)) == pytest.approx(0.30)


def test_exhausted_base_does_not_hide_remaining_refill():
    assert parse_api_allowance(allowance(base=0, refill=0.37)) == pytest.approx(0.37)


@pytest.mark.parametrize("left", [None, "0.2", True, -1, float("nan"), float("inf"), 2])
def test_invalid_numeric_allowance_is_rejected(left):
    data = allowance()
    data["consumptions"]["consumptionsForUnit"][1]["left"] = left
    with pytest.raises(UnexpectedResponseError):
        parse_api_allowance(data)


@pytest.mark.parametrize("data", [None, {}, {"consumptions": {}}, {"consumptions": {"consumptionsForUnit": []}}])
def test_missing_api_allowance_is_rejected(data):
    with pytest.raises(UnexpectedResponseError):
        parse_api_allowance(data)


def test_duplicate_aggregate_bucket_is_rejected():
    data = allowance()
    data["consumptions"]["consumptionsForUnit"].append(deepcopy(data["consumptions"]["consumptionsForUnit"][1]))
    with pytest.raises(UnexpectedResponseError, match="Duplicate"):
        parse_api_allowance(data)


@pytest.mark.parametrize(("field", "value"), [("unit", "MB"), ("type", "ROAMING_DATA"),
     ("expirationDate", None), ("expirationDate", "changed format"),
     ("expirationDate", "2099-01-01T00:00:00"), ("expirationDate", "2020-01-01T00:00:00Z")])
def test_changed_or_expired_buckets_fail_closed(field, value):
    data = allowance()
    data["consumptions"]["consumptionsForUnit"][1][field] = value
    with pytest.raises(UnexpectedResponseError):
        parse_api_allowance(data)


def test_non_data_buckets_do_not_count_as_data():
    data = allowance()
    data["consumptions"]["consumptionsForUnit"].append({"type": "VOICE", "left": 100, "unit": "MIN"})
    assert parse_api_allowance(data) == pytest.approx(0.20)


def test_expiration_checks_the_instant_in_the_supplied_timezone():
    data = allowance()
    for row in data["consumptions"]["consumptionsForUnit"]:
        row["expirationDate"] = "2030-01-01T12:00:00+02:00"
    assert parse_api_allowance(data, now=datetime(2030, 1, 1, 9, 59, tzinfo=timezone.utc)) == 0.20
    with pytest.raises(UnexpectedResponseError):
        parse_api_allowance(data, now=datetime(2030, 1, 1, 10, 0, tzinfo=timezone.utc))


@pytest.mark.parametrize(("status", "error"), [(401, AuthenticationExpiredError), (403, AuthenticationExpiredError),
     (500, UnexpectedHTTPStatusError), (429, UnexpectedHTTPStatusError), (302, UnexpectedHTTPStatusError)])
def test_http_errors_do_not_retry_or_follow_redirects(status, error):
    response = FakeResponse(status, {"errors": [{"message": "private-account-response"}]})
    client = http_client([response])
    with pytest.raises(error):
        client.activate_refill()
    assert len(client._session.calls) == 1
    assert client._session.calls[0][1]["allow_redirects"] is False
    assert response.closed


@pytest.mark.parametrize(("failure", "error"), [(requests.Timeout("private-token"), RequestTimeoutError),
     (requests.ConnectionError("private-token"), ServiceUnavailableError)])
def test_transport_failures_are_sanitized_and_not_retried(failure, error):
    client = http_client([failure])
    with pytest.raises(error) as caught:
        client.activate_refill()
    assert len(client._session.calls) == 1
    assert "private-token" not in str(caught.value)


def test_graphql_error_in_http_200_is_not_success():
    client = http_client([FakeResponse(data={"errors": [{"message": "private-account-response"}]})])
    with pytest.raises(UnexpectedResponseError) as caught:
        client.activate_refill()
    assert "private-account-response" not in str(caught.value)


def test_invalid_json_is_rejected_without_echoing_content():
    client = http_client([FakeResponse(invalid_json=True)])
    with pytest.raises(UnexpectedResponseError) as caught:
        client.get_remaining_data()
    assert "private-response-content" not in str(caught.value)


@pytest.mark.parametrize("success", [False, None, "true", 1])
def test_refill_requires_an_explicit_boolean_acknowledgement(success):
    client = http_client([FakeResponse(data={"data": {"bookTariffoptionsDirect": {"success": success}}})])
    with pytest.raises(RefillRequestError):
        client.activate_refill()


def test_har_error_after_applied_refill_is_verified_without_a_second_mutation(tmp_path, caplog):
    client = http_client([
        FakeResponse(data={"data": allowance(refill=0.20)}),
        FakeResponse(500, {"errors": [{"message": "private-response"}]}),
        FakeResponse(data={"data": allowance(refill=1.0)}),
    ])
    config = Config(client_mode="http", dry_run=False, state_db_path=tmp_path / "state.sqlite3")
    store = SQLiteStateStore(config.state_db_path)
    with caplog.at_level(logging.INFO):
        result = run_once(client, config, store, clock=lambda: 1000, sleep=lambda _: None)
    assert result.status == "confirmed"
    assert result.after_gb == 1.0
    operations = [call[1]["json"]["operationName"] for call in client._session.calls]
    assert operations == ["consumptions", "bookTariffOptionsDirect", "consumptions"]
    mutation_variables = client._session.calls[1][1]["json"]["variables"]
    assert mutation_variables == {"bookTariffoptionsDirectInput": {"bookTariffoptions": [{"tariffoptionId": FREE_REFILL_OPTION_ID}]}}
    assert store.get().status == "confirmed"
    assert "private-response" not in caplog.text
    assert "test-placeholder-token" not in caplog.text


def test_http_dry_run_fetches_allowance_without_sending_mutation(tmp_path):
    client = http_client([FakeResponse(data={"data": allowance(refill=0.20)})])
    config = Config(client_mode="http", dry_run=True, state_db_path=tmp_path / "state.sqlite3")
    result = run_once(client, config, SQLiteStateStore(config.state_db_path))
    assert result.status == "would_refill"
    assert len(client._session.calls) == 1
    assert client._session.calls[0][1]["json"]["operationName"] == "consumptions"


def test_client_uses_explicit_timeouts_and_disables_transport_retries():
    client = HttpLidlClient("test-placeholder-token", timeout_seconds=17)
    try:
        assert client._session.get_adapter("https://").max_retries.total == 0
        assert client._session.trust_env is False
    finally:
        client.close()
    client = http_client([FakeResponse(data={"data": allowance()})])
    client.get_remaining_data()
    assert client._session.calls[0][1]["timeout"] == 20
