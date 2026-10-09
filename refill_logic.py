"""One decision, at most one mutation, then bounded read-only verification."""

from dataclasses import dataclass
import logging
import math
import time
from typing import Callable

from config import Config
from lidl_client import LidlClient, LidlClientError, UnexpectedResponseError, safe_error_label
from state_store import RefillStateStore


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RunResult:
    status: str
    remaining_gb: float
    after_gb: float | None = None

    @property
    def exit_code(self) -> int:
        return 2 if self.status in {"pending", "request_uncertain", "unconfirmed"} else 0


def _read_remaining(client: LidlClient) -> float:
    value = client.get_remaining_data()
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise UnexpectedResponseError("Missing or invalid data allowance.")
    if not math.isfinite(value) or value < 0:
        raise UnexpectedResponseError("Data allowance must be finite and non-negative.")
    return float(value)


def run_once(
    client: LidlClient,
    config: Config,
    state: RefillStateStore,
    *,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
) -> RunResult:
    # Snapshot state before reading so an old allowance cannot confirm a newer claim.
    previous = state.get()
    remaining = _read_remaining(client)
    logger.info("Remaining allowance: %.3f GB", remaining)

    if previous is not None and previous.status == "pending":
        if remaining <= previous.before_gb:
            logger.warning("An earlier refill is unverified; no new refill will be requested.")
            return RunResult("pending", remaining)
        logger.info("Earlier refill confirmed by increase: %.3f GB -> %.3f GB", previous.before_gb, remaining)
        if not config.dry_run:
            state.confirm(previous.request_id)
        else:
            logger.info("Dry-run: refill state is unchanged.")

    if remaining > config.threshold_gb:
        logger.info("Above threshold %.3f GB; no refill needed.", config.threshold_gb)
        return RunResult("above_threshold", remaining)

    logger.info("Threshold reached: %.3f GB", config.threshold_gb)
    if previous is not None and clock() < previous.requested_at + config.cooldown_seconds:
        logger.info("Refill cooldown is active; no new refill will be requested.")
        return RunResult("cooldown", remaining)
    if config.dry_run:
        logger.info("Dry-run: would request exactly one free +1 GB refill.")
        return RunResult("would_refill", remaining)

    claim = state.claim(remaining, clock(), config.cooldown_seconds)
    if claim.record is None:
        logger.warning("Refill blocked by %s state from another run.", claim.blocked_by)
        return RunResult(claim.blocked_by or "pending", remaining)

    logger.info("Requesting one free +1 GB refill.")
    request_failed = False
    try:
        client.activate_refill()
    except LidlClientError as error:
        request_failed = True
        logger.warning("Refill response failed (%s); verifying allowance without resubmitting.", safe_error_label(error))

    sleep(config.verification_initial_delay)
    for attempt in range(config.verification_attempts):
        if attempt:
            sleep(config.verification_poll_interval)
        try:
            after = _read_remaining(client)
        except LidlClientError as error:
            logger.warning("Verification failed (%s); retaining pending state and not retrying.", safe_error_label(error))
            return RunResult("unconfirmed", remaining)
        if after > remaining:
            state.confirm(claim.record.request_id)
            logger.info("Refill confirmed: %.3f GB -> %.3f GB", remaining, after)
            return RunResult("confirmed", remaining, after)

    logger.warning("Allowance increase not confirmed; retaining pending state. No refill retry.")
    return RunResult("request_uncertain" if request_failed else "unconfirmed", remaining)
