"""Environment-only configuration with credentials excluded from representations."""

from dataclasses import dataclass, field
import math
import os
from pathlib import Path
from typing import Mapping


class ConfigurationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Config:
    client_mode: str = "mock"
    dry_run: bool = True
    threshold_gb: float = 0.30
    cooldown_seconds: int = 600
    verification_initial_delay: float = 12.0
    verification_poll_interval: float = 5.0
    verification_attempts: int = 3
    state_db_path: Path = Path(".state/refill.sqlite3")
    mock_remaining_data: str = "0.47 GB"
    mock_refill_outcome: str = "success"
    mock_update_after_reads: int = 0
    access_token: str | None = field(default=None, repr=False)
    token_state_key: str | None = field(default=None, repr=False)
    token_state_path: Path = Path(".state/session.sqlite3")
    http_timeout_seconds: float = 20.0

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "Config":
        env = os.environ if environ is None else environ

        def number(name: str, default: str, *, integer: bool = False) -> float | int:
            try:
                value = int(env.get(name, default)) if integer else float(env.get(name, default))
            except (ValueError, OverflowError):
                raise ConfigurationError(f"{name} must be a number.") from None
            if not math.isfinite(value) or value < 0:
                raise ConfigurationError(f"{name} must be finite and non-negative.")
            return value

        dry_run = env.get("DRY_RUN", "true").strip().lower()
        if dry_run not in {"true", "false"}:
            raise ConfigurationError("DRY_RUN must be true or false.")
        mode = env.get("CLIENT_MODE", "mock").strip().lower()
        if mode not in {"mock", "http"}:
            raise ConfigurationError("CLIENT_MODE must be mock or http.")
        outcome = env.get("MOCK_REFILL_OUTCOME", "success").strip().lower()
        if outcome not in {"success", "no_change", "timeout", "rejected"}:
            raise ConfigurationError("MOCK_REFILL_OUTCOME must be success, no_change, timeout, or rejected.")
        cooldown = int(number("REFILL_COOLDOWN_SECONDS", "600", integer=True))
        if cooldown < 600:
            raise ConfigurationError("REFILL_COOLDOWN_SECONDS must be at least 600.")
        attempts = int(number("VERIFY_ATTEMPTS", "3", integer=True))
        if not 1 <= attempts <= 10:
            raise ConfigurationError("VERIFY_ATTEMPTS must be between 1 and 10.")
        initial_delay = float(number("VERIFY_INITIAL_DELAY_SECONDS", "12"))
        interval = float(number("VERIFY_POLL_INTERVAL_SECONDS", "5"))
        if initial_delay + interval * (attempts - 1) > 300:
            raise ConfigurationError("The total verification wait must not exceed 300 seconds.")
        state_path = env.get("STATE_DB_PATH", ".state/refill.sqlite3").strip()
        if not state_path or state_path == ":memory:":
            raise ConfigurationError("STATE_DB_PATH must name a persistent SQLite file.")
        token = env.get("LIDL_ACCESS_TOKEN") or env.get("LIDL_SESSION_TOKEN") or None
        if token is not None and (not token.isascii() or any(char.isspace() or ord(char) < 33 for char in token)):
            raise ConfigurationError("LIDL_ACCESS_TOKEN must be a raw token without whitespace or control characters.")
        http_timeout = float(number("HTTP_TIMEOUT_SECONDS", "20"))
        if not 0 < http_timeout <= 120:
            raise ConfigurationError("HTTP_TIMEOUT_SECONDS must be greater than 0 and at most 120.")
        token_path = env.get("LIDL_TOKEN_STATE_PATH", ".state/session.sqlite3").strip()
        if not token_path or token_path == ":memory:":
            raise ConfigurationError("LIDL_TOKEN_STATE_PATH must name a persistent SQLite file.")
        if Path(token_path).resolve() == Path(state_path).resolve():
            raise ConfigurationError("Session and refill state must use separate SQLite files.")
        return cls(
            client_mode=mode,
            dry_run=dry_run == "true",
            threshold_gb=float(number("REFILL_THRESHOLD_GB", "0.30")),
            cooldown_seconds=cooldown,
            verification_initial_delay=initial_delay,
            verification_poll_interval=interval,
            verification_attempts=attempts,
            state_db_path=Path(state_path),
            mock_remaining_data=env.get("MOCK_REMAINING_DATA", "0.47 GB"),
            mock_refill_outcome=outcome,
            mock_update_after_reads=int(number("MOCK_UPDATE_AFTER_READS", "0", integer=True)),
            access_token=token,
            token_state_key=env.get("LIDL_TOKEN_STATE_KEY") or None,
            token_state_path=Path(token_path),
            http_timeout_seconds=http_timeout,
        )
