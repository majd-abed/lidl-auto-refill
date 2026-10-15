"""HTTP operations captured from the portal and the user's HAR, 2026-10-09."""

from datetime import datetime, timedelta, timezone
import logging
import math
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests

from authentication import request_tokens
from session_store import RenewableSessionStore, SessionTokens

from lidl_client import (
    AllowancePeriodExpiredError, AuthenticationExpiredError, LidlClientError,
    RefillRequestError, RequestTimeoutError, ServiceUnavailableError,
    UnexpectedHTTPStatusError, UnexpectedResponseError, safe_error_label,
)


logger = logging.getLogger(__name__)

GRAPHQL_URL = "https://api.lidl-connect.de/api/graphql"
# This identifier was captured from the action the user confirmed was free +1 GB.
FREE_REFILL_OPTION_ID = "CCS_92061"

CONSUMPTIONS_QUERY = """query consumptions {
  consumptions {
    consumptionsForUnit {
      consumed
      unit
      formattedUnit
      type
      description
      expirationDate
      left
      max
      tariffOrOptions {
        name
        id
        type
        consumptions {
          consumed
          unit
          formattedUnit
          type
          description
          expirationDate
          left
          max
          __typename
        }
        __typename
      }
      __typename
    }
    __typename
  }
}
"""

REFILL_MUTATION = """mutation bookTariffOptionsDirect($bookTariffoptionsDirectInput: BookTariffoptionsDirectInput!) {
  bookTariffoptionsDirect(
    bookTariffoptionsDirectInput: $bookTariffoptionsDirectInput
  ) {
    success
    __typename
  }
}
"""


BOOKED_TARIFF_QUERY = """query bookedTariff {
  tariffs {
    bookedTariff {
      statusKey
      isSuspendedActive
      isPendingTariff
      renewContractDate
    }
  }
}
"""


def _german_midnight(value: datetime) -> bool:
    if value.utcoffset() not in {timedelta(hours=1), timedelta(hours=2)} or any((value.hour, value.minute, value.second, value.microsecond)):
        return False
    try:
        berlin = value.astimezone(ZoneInfo("Europe/Berlin"))
    except ZoneInfoNotFoundError:
        return False
    return (
        berlin.replace(tzinfo=None) == value.replace(tzinfo=None)
        and berlin.utcoffset() == value.utcoffset()
    )


def _common_midnight_expiry(expirations: list[datetime]) -> datetime | None:
    first = expirations[0]
    if not _german_midnight(first):
        return None
    if any(
        value.date() != first.date() or value.utcoffset() != first.utcoffset() or not _german_midnight(value)
        for value in expirations[1:]
    ):
        return None
    return first


def _tariff_covers_last_day(expiry: datetime, now: datetime, context: object) -> bool:
    if not isinstance(context, dict) or not isinstance(context.get("tariffs"), dict):
        return False
    tariff = context["tariffs"].get("bookedTariff")
    if (
        not isinstance(tariff, dict)
        or tariff.get("statusKey") != "CDL"
        or tariff.get("isSuspendedActive") is not False
        or tariff.get("isPendingTariff") is not False
    ):
        return False
    raw_renewal = tariff.get("renewContractDate")
    if not isinstance(raw_renewal, str):
        return False
    try:
        renewal = datetime.fromisoformat(raw_renewal.replace("Z", "+00:00"))
    except ValueError:
        return False
    return (
        _german_midnight(renewal)
        and renewal.date() == expiry.date() + timedelta(days=1)
        and expiry <= now < renewal
    )


def parse_api_allowance(data: object, *, now: datetime | None = None, tariff_context: object = None) -> float:
    """Sum aggregate buckets; require matching tariff metadata for an inclusive final date."""
    if not isinstance(data, dict) or not isinstance(data.get("consumptions"), dict):
        raise UnexpectedResponseError("Missing consumptions object.")
    rows = data["consumptions"].get("consumptionsForUnit")
    if not isinstance(rows, list) or not rows:
        raise UnexpectedResponseError("Missing allowance rows.")
    current_time = now or datetime.now(timezone.utc)
    seen = set()
    total = 0.0
    expired_count = 0
    expirations: list[datetime] = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("type"), str):
            raise UnexpectedResponseError("Invalid allowance row.")
        row_type = row["type"]
        if row_type not in {"DATA", "REFILLABLE_DATA"}:
            if "DATA" in row_type.upper():
                raise UnexpectedResponseError("Unrecognized data bucket type; inspect the new response format.")
            continue
        if row_type in seen:
            raise UnexpectedResponseError("Duplicate aggregate data bucket; refusing to double-count allowance.")
        seen.add(row_type)
        if row.get("unit") != "GB":
            raise UnexpectedResponseError("Unrecognized API data unit; only the captured GB format is supported.")
        left, maximum = row.get("left"), row.get("max")
        for value in (left, maximum):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise UnexpectedResponseError("Invalid numeric API allowance.")
        if left > maximum + 1e-9:
            raise UnexpectedResponseError("API remaining allowance exceeds its maximum.")
        expiration = row.get("expirationDate")
        if not isinstance(expiration, str):
            raise UnexpectedResponseError("Missing data bucket expiration.")
        try:
            expiry = datetime.fromisoformat(expiration.replace("Z", "+00:00"))
        except ValueError:
            raise UnexpectedResponseError("Invalid data bucket expiration format.") from None
        if expiry.tzinfo is None:
            raise UnexpectedResponseError("Expired or ambiguous data bucket; request a fresh allowance snapshot.")
        expirations.append(expiry)
        if expiry <= current_time:
            expired_count += 1
        total += left
    if not seen or not math.isfinite(total):
        raise UnexpectedResponseError("No valid data allowance found.")
    if expired_count == len(seen):
        last_day_expiry = _common_midnight_expiry(expirations)
        if last_day_expiry is not None and _tariff_covers_last_day(last_day_expiry, current_time, tariff_context):
            return total
        raise AllowancePeriodExpiredError("All reported allowance buckets have expired; waiting for an active period.", last_day_expiry=last_day_expiry)
    if expired_count:
        raise UnexpectedResponseError("Expired or ambiguous data bucket; request a fresh allowance snapshot.")
    return total


class HttpLidlClient:
    def __init__(self, access_token: str | None = None, timeout_seconds: float = 20.0, *, session_store: RenewableSessionStore | None = None) -> None:
        self._token = access_token
        self._token_store = session_store
        self._timeout = timeout_seconds
        self._session = requests.Session()
        self._session.trust_env = False
        self._session.mount("https://", requests.adapters.HTTPAdapter(max_retries=0))

    def _renew(self, tokens: SessionTokens) -> SessionTokens:
        return request_tokens(self._session, {
            "grant_type": "refresh_token", "client_id": tokens.client_id,
            "client_secret": tokens.client_secret, "refresh_token": tokens.refresh_token,
        }, timeout=self._timeout, client_id=tokens.client_id, client_secret=tokens.client_secret)

    def prepare_session(self) -> None:
        """Resolve authentication before the controller can consider a refill."""
        if self._token_store is not None:
            self._token = self._token_store.access_token(self._renew)

    def _graphql(self, operation: str, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        self.prepare_session()
        if not self._token:
            raise AuthenticationExpiredError("No authenticated session is available.")
        try:
            response = self._session.post(
                GRAPHQL_URL,
                json={"operationName": operation, "variables": variables, "query": query},
                headers={"Authorization": "Bearer " + self._token, "Content-Type": "application/json"},
                timeout=self._timeout,
                allow_redirects=False,
            )
        except requests.Timeout:
            raise RequestTimeoutError("Lidl request timed out.") from None
        except requests.RequestException:
            raise ServiceUnavailableError("Unable to complete the Lidl request.") from None
        try:
            if response.status_code in {401, 403}:
                raise AuthenticationExpiredError("Lidl rejected the authenticated session.", response.status_code)
            if response.status_code != 200:
                raise UnexpectedHTTPStatusError(response.status_code)
            try:
                payload = response.json()
            except ValueError:
                raise UnexpectedResponseError("Lidl returned an invalid JSON response.") from None
            if not isinstance(payload, dict) or payload.get("errors"):
                raise UnexpectedResponseError("Lidl returned a GraphQL error or changed response format.")
            if not isinstance(payload.get("data"), dict):
                raise UnexpectedResponseError("Lidl returned no GraphQL data.")
            return payload["data"]
        finally:
            response.close()

    def get_remaining_data(self) -> float:
        try:
            data = self._graphql("consumptions", CONSUMPTIONS_QUERY, {})
        except AuthenticationExpiredError as error:
            if error.status_code != 401 or self._token_store is None:
                raise
            self._token = self._token_store.access_token(self._renew, rejected_token=self._token)
            # Only this read may be retried. Refill mutations are never replayed.
            data = self._graphql("consumptions", CONSUMPTIONS_QUERY, {})
        now = datetime.now(timezone.utc)
        try:
            return parse_api_allowance(data, now=now)
        except AllowancePeriodExpiredError as error:
            expiry = error.last_day_expiry
            # This only bounds the extra read; matching renewal metadata is
            # still required, including on a 23- or 25-hour local calendar day.
            if expiry is None or not timedelta(0) <= now - expiry < timedelta(hours=25):
                raise
            try:
                context = self._graphql("bookedTariff", BOOKED_TARIFF_QUERY, {})
            except LidlClientError as context_error:
                logger.warning("Tariff status unavailable (%s); waiting for current allowance data.", safe_error_label(context_error))
                raise error from None
            return parse_api_allowance(data, tariff_context=context)

    def activate_refill(self) -> None:
        data = self._graphql("bookTariffOptionsDirect", REFILL_MUTATION, {
            "bookTariffoptionsDirectInput": {"bookTariffoptions": [{"tariffoptionId": FREE_REFILL_OPTION_ID}]},
        })
        result = data.get("bookTariffoptionsDirect")
        if not isinstance(result, dict) or result.get("success") is not True:
            raise RefillRequestError("Lidl did not acknowledge the refill; verify the allowance before any further action.")

    def close(self) -> None:
        self._session.close()
