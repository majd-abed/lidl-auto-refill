"""Run a single safe check; scheduling is external to this process."""

import logging
import sys
import time

from config import Config, ConfigurationError
from lidl_client import ClientNotConfiguredError, LidlClientError, create_client, safe_error_label
from refill_logic import run_once
from state_store import SQLiteStateStore, StateStoreError
from session_store import SessionStateError


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
    try:
        config = Config.from_env()
        client = create_client(config)
        if config.client_mode == "mock":
            logger.warning("MOCK mode: no Lidl account is contacted; all allowance/refill results are simulated.")
        result = run_once(client, config, SQLiteStateStore(config.state_db_path))
        return result.exit_code
    except (ConfigurationError, StateStoreError, ClientNotConfiguredError) as error:
        logger.error("%s", error)
        return 1
    except SessionStateError as error:
        logger.error("Session unavailable: %s No refill requested.", error)
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
