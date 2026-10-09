"""Client contract, display parser, simulations, and client selection."""

import math
import re
from typing import Protocol

from config import Config


class LidlClientError(RuntimeError):
    """Client failures must never contain account responses or authentication data."""


class AuthenticationExpiredError(LidlClientError):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        self.status_code = status_code
        super().__init__(message)


class ServiceUnavailableError(LidlClientError):
    pass


class RequestTimeoutError(LidlClientError):
    pass


class UnexpectedResponseError(LidlClientError):
    pass


class RefillRequestError(LidlClientError):
    pass


class ClientNotConfiguredError(LidlClientError):
    pass


class UnexpectedHTTPStatusError(LidlClientError):
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        super().__init__("Unexpected HTTP status from Lidl.")


def safe_error_label(error: LidlClientError) -> str:
    status = getattr(error, "status_code", None)
    return f"{type(error).__name__}, HTTP {status}" if isinstance(status, int) else type(error).__name__


class AllowanceParseError(UnexpectedResponseError, ValueError):
    pass


def parse_data_allowance(text: str) -> float:
    """Parse one MB/GB value; Phase 1 uses 1 GB = 1024 MB as requested."""
    if not isinstance(text, str):
        raise AllowanceParseError("Expected a data allowance string.")
    match = re.fullmatch(r"\s*([0-9]+(?:[.,][0-9]+)?)\s*(MB|GB)\s*", text, re.IGNORECASE)
    if match is None:
        raise AllowanceParseError("Expected a numeric data allowance with an MB or GB unit.")
    value = float(match.group(1).replace(",", "."))
    if not math.isfinite(value):
        raise AllowanceParseError("Data allowance must be finite.")
    return value / 1024 if match.group(2).upper() == "MB" else value


class LidlClient(Protocol):
    def get_remaining_data(self) -> float:
        """Return GB; missing, expired, or changed data must raise LidlClientError."""
        ...

    def activate_refill(self) -> None:
        """Request one free +1 GB refill, with no automatic mutation retries."""
        ...

    def close(self) -> None:
        ...


class MockLidlClient:
    """In-memory simulation; never contacts Lidl, even when DRY_RUN=false."""

    def __init__(self, remaining_gb: float, outcome: str = "success", update_after_reads: int = 0) -> None:
        self.remaining_gb = remaining_gb
        self.outcome = outcome
        self.update_after_reads = update_after_reads
        self.refill_calls = 0
        self._pending_reads: int | None = None

    def get_remaining_data(self) -> float:
        if self._pending_reads is not None:
            if self._pending_reads == 0:
                self.remaining_gb += 1.0
                self._pending_reads = None
            else:
                self._pending_reads -= 1
        return self.remaining_gb

    def activate_refill(self) -> None:
        self.refill_calls += 1
        if self.outcome in {"success", "timeout"}:
            self._pending_reads = self.update_after_reads
        if self.outcome == "timeout":
            # The backend may have accepted the request before the connection timed out.
            raise RequestTimeoutError("Simulated ambiguous refill timeout.")
        if self.outcome == "rejected":
            raise RefillRequestError("Simulated refill rejection.")

    def close(self) -> None:
        pass


def create_client(config: Config) -> LidlClient:
    if config.client_mode == "http":
        from http_client import HttpLidlClient
        if config.token_state_key:
            from session_store import SessionStore
            store = SessionStore(config.token_state_path, config.token_state_key, lock_timeout=config.http_timeout_seconds + 20)
            return HttpLidlClient(timeout_seconds=config.http_timeout_seconds, session_store=store)
        if not config.access_token:
            raise ClientNotConfiguredError("Set LIDL_TOKEN_STATE_KEY after bootstrapping a session, or supply LIDL_ACCESS_TOKEN for one short run.")
        return HttpLidlClient(config.access_token, config.http_timeout_seconds)
    return MockLidlClient(
        parse_data_allowance(config.mock_remaining_data),
        config.mock_refill_outcome,
        config.mock_update_after_reads,
    )
