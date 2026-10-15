from copy import deepcopy
from datetime import datetime, timezone
import logging
from unittest.mock import Mock
from zoneinfo import ZoneInfoNotFoundError

import pytest
import requests

from config import Config
from http_client import FREE_REFILL_OPTION_ID, HttpLidlClient, parse_api_allowance
from lidl_client import (
    AllowancePeriodExpiredError, AuthenticationExpiredError, RefillRequestError, RequestTimeoutError,
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


def dated_allowance(expiration="2026-10-14T00:00:00+02:00", *, refill=0.88):
    data = allowance(refill=refill)
    for row in data["consumptions"]["consumptionsForUnit"]:
        row["expirationDate"] = expiration
    return data


def booked_tariff_context(renewal="2026-10-15T00:00:00+02:00", **fields):
    return {"tariffs": {"bookedTariff": {
        "statusKey": "CDL", "isSuspendedActive": False, "isPendingTariff": False,
        "renewContractDate": renewal, **fields,
    }}}


def freeze_http_time(monkeypatch, now):
    clock = Mock(wraps=datetime)
    clock.now.return_value = now
    monkeypatch.setattr("http_client.datetime", clock)
    return clock


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
    with pytest.raises(UnexpectedResponseError) as caught:
        parse_api_allowance(data)
    assert not isinstance(caught.value, AllowancePeriodExpiredError)


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
    with pytest.raises(AllowancePeriodExpiredError):
        parse_api_allowance(data, now=datetime(2030, 1, 1, 10, 0, tzinfo=timezone.utc))


@pytest.mark.parametrize("refill", [0.0, 0.88])
def test_all_expired_buckets_report_an_ended_period_without_a_current_balance(refill):
    data = allowance(base=0, refill=refill)
    for row in data["consumptions"]["consumptionsForUnit"]:
        row["expirationDate"] = "2026-10-14T00:00:00+02:00"
    with pytest.raises(AllowancePeriodExpiredError):
        parse_api_allowance(data, now=datetime(2026, 10, 14, 11, 40, tzinfo=timezone.utc))


@pytest.mark.parametrize(("field", "value"), [
    ("unit", "MB"), ("type", "ROAMING_DATA"), ("left", None),
    ("left", "0.88"), ("left", True), ("left", -1),
    ("left", float("nan")), ("left", float("inf")), ("left", 2),
    ("max", None), ("max", True), ("max", -1),
    ("expirationDate", None), ("expirationDate", "changed format"),
    ("expirationDate", "2026-10-14T00:00:00"),
])
def test_an_expired_bucket_does_not_hide_a_later_invalid_row(field, value):
    data = allowance(refill=0.88)
    for row in data["consumptions"]["consumptionsForUnit"]:
        row["expirationDate"] = "2026-10-14T00:00:00+02:00"
    data["consumptions"]["consumptionsForUnit"][1][field] = value
    with pytest.raises(UnexpectedResponseError) as caught:
        parse_api_allowance(data, now=datetime(2026, 10, 14, 11, 40, tzinfo=timezone.utc))
    assert not isinstance(caught.value, AllowancePeriodExpiredError)


def test_expired_duplicate_buckets_remain_a_hard_error():
    data = allowance(refill=0.88)
    rows = data["consumptions"]["consumptionsForUnit"]
    for row in rows:
        row["expirationDate"] = "2026-10-14T00:00:00+02:00"
    rows.append(deepcopy(rows[1]))
    with pytest.raises(UnexpectedResponseError, match="Duplicate") as caught:
        parse_api_allowance(data, now=datetime(2026, 10, 14, 11, 40, tzinfo=timezone.utc))
    assert not isinstance(caught.value, AllowancePeriodExpiredError)


def test_non_data_rows_alone_do_not_report_an_ended_allowance_period():
    data = {"consumptions": {"consumptionsForUnit": [{"type": "VOICE", "left": 0, "unit": "MIN"}]}}
    with pytest.raises(UnexpectedResponseError) as caught:
        parse_api_allowance(data)
    assert not isinstance(caught.value, AllowancePeriodExpiredError)


@pytest.mark.parametrize("expired_index", [0, 1])
def test_mixed_active_and_expired_buckets_remain_a_hard_error(expired_index):
    data = allowance(refill=0.88)
    data["consumptions"]["consumptionsForUnit"][expired_index]["expirationDate"] = "2026-10-14T00:00:00+02:00"
    with pytest.raises(UnexpectedResponseError) as caught:
        parse_api_allowance(data, now=datetime(2026, 10, 14, 11, 40, tzinfo=timezone.utc), tariff_context=booked_tariff_context())
    assert not isinstance(caught.value, AllowancePeriodExpiredError)


def test_a_later_active_period_is_read_without_reusing_the_expired_balance(monkeypatch):
    freeze_http_time(monkeypatch, datetime(2026, 10, 16, tzinfo=timezone.utc))
    expired = allowance(refill=0.88)
    for row in expired["consumptions"]["consumptionsForUnit"]:
        row["expirationDate"] = "2026-10-14T00:00:00+02:00"
    client = http_client([
        FakeResponse(data={"data": expired}),
        FakeResponse(data={"data": allowance(base=24.22, refill=0)}),
    ])
    with pytest.raises(AllowancePeriodExpiredError):
        client.get_remaining_data()
    assert client.get_remaining_data() == pytest.approx(24.22)
    assert [call[1]["json"]["operationName"] for call in client._session.calls] == ["consumptions", "consumptions"]


@pytest.mark.parametrize("now", [
    datetime(2026, 10, 13, 22, tzinfo=timezone.utc),
    datetime(2026, 10, 14, 11, 40, tzinfo=timezone.utc),
])
@pytest.mark.parametrize("refill", [0.0, 0.88])
def test_matching_active_tariff_confirms_the_inclusive_last_calendar_day(now, refill):
    assert parse_api_allowance(dated_allowance(refill=refill), now=now, tariff_context=booked_tariff_context()) == pytest.approx(refill)


@pytest.mark.parametrize("now", [
    datetime(2026, 10, 14, 22, tzinfo=timezone.utc),
    datetime(2026, 10, 15, 11, 40, tzinfo=timezone.utc),
])
def test_last_day_context_never_extends_a_balance_to_or_past_renewal(now):
    with pytest.raises(AllowancePeriodExpiredError):
        parse_api_allowance(dated_allowance(), now=now, tariff_context=booked_tariff_context())


def test_a_new_tariff_period_does_not_authorize_an_old_allowance_snapshot():
    with pytest.raises(AllowancePeriodExpiredError):
        parse_api_allowance(dated_allowance(), now=datetime(2026, 10, 14, 11, 40, tzinfo=timezone.utc), tariff_context=booked_tariff_context("2026-11-12T00:00:00+01:00"))


@pytest.mark.parametrize(("expiry", "renewal", "now"), [
    ("2026-10-25T00:00:00+02:00", "2026-10-26T00:00:00+01:00", datetime(2026, 10, 25, 22, 30, tzinfo=timezone.utc)),
    ("2026-03-29T00:00:00+01:00", "2026-03-30T00:00:00+02:00", datetime(2026, 3, 29, 21, 30, tzinfo=timezone.utc)),
])
def test_last_day_uses_calendar_dates_across_both_dst_changes(expiry, renewal, now):
    data = dated_allowance(expiry)
    context = booked_tariff_context(renewal)
    assert parse_api_allowance(data, now=now, tariff_context=context) == pytest.approx(0.88)
    with pytest.raises(AllowancePeriodExpiredError):
        parse_api_allowance(data, now=datetime.fromisoformat(renewal), tariff_context=context)


@pytest.mark.parametrize(("expiry", "renewal", "now"), [
    ("2026-10-14T00:00:00+02:00", "2026-10-15T00:00:00+01:00", datetime(2026, 10, 14, 22, 30, tzinfo=timezone.utc)),
    ("2026-10-14T00:00:00+01:00", "2026-10-15T00:00:00+02:00", datetime(2026, 10, 14, 11, 40, tzinfo=timezone.utc)),
    ("2026-11-11T00:00:00+02:00", "2026-11-12T00:00:00+01:00", datetime(2026, 11, 11, 12, tzinfo=timezone.utc)),
])
def test_wrong_seasonal_offsets_cannot_extend_or_authorize_last_day_data(expiry, renewal, now):
    with pytest.raises(AllowancePeriodExpiredError):
        parse_api_allowance(dated_allowance(expiry), now=now, tariff_context=booked_tariff_context(renewal))


def test_missing_berlin_timezone_data_keeps_waiting_without_a_tariff_request(monkeypatch):
    freeze_http_time(monkeypatch, datetime(2026, 10, 14, 11, 40, tzinfo=timezone.utc))
    monkeypatch.setattr("http_client.ZoneInfo", Mock(side_effect=ZoneInfoNotFoundError("Europe/Berlin")))
    client = http_client([FakeResponse(data={"data": dated_allowance()})])
    with pytest.raises(AllowancePeriodExpiredError):
        client.get_remaining_data()
    assert len(client._session.calls) == 1


@pytest.mark.parametrize(("field", "value"), [
    ("statusKey", None), ("statusKey", "OTHER"),
    ("isSuspendedActive", True), ("isSuspendedActive", 0), ("isSuspendedActive", None),
    ("isPendingTariff", True), ("isPendingTariff", 0), ("isPendingTariff", "false"),
    ("renewContractDate", None), ("renewContractDate", "changed format"),
    ("renewContractDate", "2026-10-15T00:00:00"),
    ("renewContractDate", "2026-10-14T00:00:00+02:00"),
    ("renewContractDate", "2026-10-16T00:00:00+02:00"),
    ("renewContractDate", "2026-10-15T01:00:00+02:00"),
    ("renewContractDate", "2026-10-15T00:00:00+03:00"),
    ("renewContractDate", "2026-10-15T00:00:00+01:00"),
    ("renewContractDate", "2026-10-15T00:00:00Z"),
])
def test_invalid_tariff_metadata_cannot_authorize_expired_buckets(field, value):
    context = booked_tariff_context()
    context["tariffs"]["bookedTariff"][field] = value
    with pytest.raises(AllowancePeriodExpiredError):
        parse_api_allowance(dated_allowance(), now=datetime(2026, 10, 14, 11, 40, tzinfo=timezone.utc), tariff_context=context)


@pytest.mark.parametrize("context", [None, {}, [], {"tariffs": None}, {"tariffs": {"bookedTariff": []}}, {"tariffs": {"bookedTariff": {}}}])
def test_missing_tariff_metadata_keeps_the_waiting_condition(context):
    with pytest.raises(AllowancePeriodExpiredError):
        parse_api_allowance(dated_allowance(), now=datetime(2026, 10, 14, 11, 40, tzinfo=timezone.utc), tariff_context=context)


@pytest.mark.parametrize("expiry", [
    "2026-10-14T00:00:00Z", "2026-10-14T00:00:00+03:00",
    "2026-10-14T00:00:00+01:00",
    "2026-10-14T00:00:01+02:00", "2026-10-14T00:00:00.001+02:00",
])
def test_non_german_or_non_midnight_expiry_cannot_use_last_day_context(expiry):
    with pytest.raises(AllowancePeriodExpiredError):
        parse_api_allowance(dated_allowance(expiry), now=datetime(2026, 10, 14, 11, 40, tzinfo=timezone.utc), tariff_context=booked_tariff_context())


@pytest.mark.parametrize("second_expiry", ["2026-10-14T00:00:00+01:00", "2026-10-13T00:00:00+02:00"])
def test_different_bucket_dates_or_offsets_cannot_use_last_day_context(second_expiry):
    data = dated_allowance()
    data["consumptions"]["consumptionsForUnit"][1]["expirationDate"] = second_expiry
    with pytest.raises(AllowancePeriodExpiredError):
        parse_api_allowance(data, now=datetime(2026, 10, 14, 11, 40, tzinfo=timezone.utc), tariff_context=booked_tariff_context())


def test_http_reads_only_captured_tariff_status_for_a_possible_last_day(monkeypatch):
    freeze_http_time(monkeypatch, datetime(2026, 10, 14, 11, 40, tzinfo=timezone.utc))
    client = http_client([
        FakeResponse(data={"data": dated_allowance()}),
        FakeResponse(data={"data": booked_tariff_context()}),
    ])
    assert client.get_remaining_data() == pytest.approx(0.88)
    assert [call[1]["json"]["operationName"] for call in client._session.calls] == ["consumptions", "bookedTariff"]
    assert client._session.calls[1][1]["json"]["variables"] == {}


@pytest.mark.parametrize(("expiry", "now"), [
    ("2026-10-12T00:00:00+02:00", datetime(2026, 10, 14, 11, 40, tzinfo=timezone.utc)),
    ("2026-10-14T00:00:01+02:00", datetime(2026, 10, 14, 11, 40, tzinfo=timezone.utc)),
    ("2026-10-14T00:00:00Z", datetime(2026, 10, 14, 11, 40, tzinfo=timezone.utc)),
    ("2026-10-14T00:00:00+02:00", datetime(2026, 10, 14, 23, tzinfo=timezone.utc)),
])
def test_http_does_not_query_tariff_for_ineligible_expired_snapshots(monkeypatch, expiry, now):
    freeze_http_time(monkeypatch, now)
    client = http_client([FakeResponse(data={"data": dated_allowance(expiry)})])
    with pytest.raises(AllowancePeriodExpiredError):
        client.get_remaining_data()
    assert len(client._session.calls) == 1


@pytest.mark.parametrize(("field", "value"), [("expirationDate", "2099-01-01T00:00:00+02:00"), ("unit", "MB")])
def test_http_does_not_query_tariff_for_mixed_or_invalid_buckets(monkeypatch, field, value):
    freeze_http_time(monkeypatch, datetime(2026, 10, 14, 11, 40, tzinfo=timezone.utc))
    data = dated_allowance()
    data["consumptions"]["consumptionsForUnit"][1][field] = value
    client = http_client([FakeResponse(data={"data": data})])
    with pytest.raises(UnexpectedResponseError) as caught:
        client.get_remaining_data()
    assert not isinstance(caught.value, AllowancePeriodExpiredError)
    assert len(client._session.calls) == 1


@pytest.mark.parametrize("response", [
    FakeResponse(status=503, data={"private-response": "private-value"}),
    FakeResponse(invalid_json=True),
    FakeResponse(data={"errors": [{"message": "private-response-content"}]}),
    requests.Timeout("private-transport-value"),
])
def test_failed_tariff_read_keeps_waiting_without_private_response_logs(monkeypatch, caplog, response):
    freeze_http_time(monkeypatch, datetime(2026, 10, 14, 11, 40, tzinfo=timezone.utc))
    client = http_client([FakeResponse(data={"data": dated_allowance()}), response])
    with caplog.at_level(logging.WARNING), pytest.raises(AllowancePeriodExpiredError):
        client.get_remaining_data()
    assert [call[1]["json"]["operationName"] for call in client._session.calls] == ["consumptions", "bookedTariff"]
    assert "Tariff status unavailable" in caplog.text
    assert "private-" not in caplog.text


def test_http_rechecks_time_after_reading_tariff_context(monkeypatch):
    clock = freeze_http_time(monkeypatch, datetime(2026, 10, 14, 21, 59, 59, tzinfo=timezone.utc))
    clock.now.side_effect = [datetime(2026, 10, 14, 21, 59, 59, tzinfo=timezone.utc), datetime(2026, 10, 14, 22, tzinfo=timezone.utc)]
    client = http_client([
        FakeResponse(data={"data": dated_allowance()}),
        FakeResponse(data={"data": booked_tariff_context()}),
    ])
    with pytest.raises(AllowancePeriodExpiredError):
        client.get_remaining_data()


def test_last_day_allowance_can_refill_once_and_verify_with_read_only_context(monkeypatch, tmp_path):
    freeze_http_time(monkeypatch, datetime(2026, 10, 14, 11, 40, tzinfo=timezone.utc))
    client = http_client([
        FakeResponse(data={"data": dated_allowance(refill=0.20)}),
        FakeResponse(data={"data": booked_tariff_context()}),
        FakeResponse(data={"data": {"bookTariffOptionsDirect": {"success": True}}}),
        FakeResponse(data={"data": dated_allowance(refill=1.0)}),
        FakeResponse(data={"data": booked_tariff_context()}),
    ])
    config = Config(client_mode="http", dry_run=False, state_db_path=tmp_path / "state.sqlite3")
    store = SQLiteStateStore(config.state_db_path)
    result = run_once(client, config, store, sleep=lambda _: None)
    assert result.status == "confirmed"
    assert result.after_gb == 1.0
    assert store.get().status == "confirmed"
    assert [call[1]["json"]["operationName"] for call in client._session.calls] == ["consumptions", "bookedTariff", "bookTariffOptionsDirect", "consumptions", "bookedTariff"]


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
