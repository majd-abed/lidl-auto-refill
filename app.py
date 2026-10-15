"""Run a single safe check; scheduling is external to this process."""

import logging
import os
from pathlib import Path
import sys
import time
from typing import cast

from config import Config, ConfigurationError
from lidl_client import ClientNotConfiguredError, LidlClient, LidlClientError, create_client, safe_error_label
from http_client import HttpLidlClient
from refill_logic import run_once
from state_store import StateStoreError, create_state_store
from session_store import SessionRenewalInterrupted, SessionStateError


class SessionRecoveryFailed(SessionStateError):
    """The single normal sign-in recovery attempt could not be completed."""


def write_period_waiting_summary(logger: logging.Logger, *, pending: bool) -> None:
    target = os.environ.get("GITHUB_STEP_SUMMARY")
    if not target:
        return
    text = "### Waiting for current allowance data\n\nLidl reports only expired data buckets. No current GB balance was assumed and no refill was requested.\n\n"
    text += "An earlier refill is still unverified and remains blocked.\n" if pending else "While the timer is enabled, the next five-minute check will try again automatically.\n"
    try:
        with Path(target).open("a", encoding="utf-8") as summary:
            summary.write(text)
    except OSError:
        logger.warning("Could not write the workflow summary; the period status is available in the log.")


def create_client_with_recovery(config: Config, logger: logging.Logger) -> LidlClient:
    client = create_client(config)
    try:
        if config.client_mode == "http":
            cast(HttpLidlClient, client).prepare_session()
    except SessionRenewalInterrupted:
        client.close()
        if config.client_mode != "http" or not os.environ.get("LIDL_USERNAME") or not os.environ.get("LIDL_PASSWORD"):
            raise SessionRecoveryFailed("Interrupted renewal requires normal login credentials in Secrets.") from None
        from authentication import bootstrap_from_environment
        logger.warning("Previous token renewal was interrupted; attempting one normal sign-in without replaying the refresh request.")
        try:
            bootstrap_from_environment(config, replace=True)
            client = create_client(config)
            cast(HttpLidlClient, client).prepare_session()
        except Exception as error:
            client.close()
            label = safe_error_label(error) if isinstance(error, LidlClientError) else type(error).__name__
            raise SessionRecoveryFailed("Normal sign-in recovery failed (" + label + "). Complete normal sign-in before resuming.") from None
        logger.info("Session recovered through normal sign-in; continuing the allowance check.")
    except Exception:
        client.close()
        raise
    return client


def pause_after_failed_recovery(config: Config | None, logger: logging.Logger) -> None:
    if config is None or config.state_backend != "postgres" or os.environ.get("CLOUD_SCHEDULED_CHECK") != "true":
        return
    from cloud_scheduler import pause
    from postgres_store import _Postgres
    try:
        store = _Postgres(config.database_url, config.state_account_key, config.database_ca_cert)
        with store._database() as connection, connection.transaction():
            connection.execute("SET LOCAL statement_timeout = '20s'")
            connection.execute("SET LOCAL lock_timeout = '10s'")
            connection.execute("SELECT pg_advisory_xact_lock(1818846316, 1)")
            pause(connection)
        logger.error("Automatic timer paused after failed sign-in recovery. Resolve the session, then enable the cloud timer to resume.")
    except StateStoreError:
        logger.error("Could not pause the automatic timer. Pause it in Supabase Cron before retrying sign-in.")


def configure_logging() -> None:
    handler = logging.StreamHandler()
    formatter = logging.Formatter("%(asctime)s UTC %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    formatter.converter = time.gmtime
    handler.setFormatter(formatter)
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)


def main() -> int:
    configure_logging()
    logger = logging.getLogger(__name__)
    if sys.version_info < (3, 12):
        logger.error("Python 3.12 or newer is required.")
        return 1
    client = None
    config = None
    try:
        config = Config.from_env()
        client = create_client_with_recovery(config, logger)
        if config.client_mode == "mock":
            logger.warning("MOCK mode: no Lidl account is contacted; all allowance/refill results are simulated.")
        result = run_once(client, config, create_state_store(config))
        if result.remaining_gb is None:
            write_period_waiting_summary(logger, pending=result.status == "pending")
        return result.exit_code
    except (ConfigurationError, StateStoreError, ClientNotConfiguredError) as error:
        logger.error("%s", error)
        return 1
    except SessionStateError as error:
        logger.error("Session unavailable: %s No refill requested.", error)
        if isinstance(error, SessionRecoveryFailed):
            pause_after_failed_recovery(config, logger)
        return 1
    except LidlClientError as error:
        logger.error("Allowance retrieval failed (%s); no refill requested.", safe_error_label(error))
        return 1
    except KeyboardInterrupt:
        logger.warning("Interrupted. Any recorded pending refill remains blocked.")
        return 130
    except Exception as error:
        # Avoid tracebacks or exception text that could contain future HTTP credentials.
        logger.error("Unexpected failure (%s). Inspect state before attempting a refill again.", type(error).__name__)
        return 1
    finally:
        if client is not None:
            client.close()


if __name__ == "__main__":
    raise SystemExit(main())
