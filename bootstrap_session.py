"""Explicit normal sign-in; credentials come from the environment only."""

import argparse
import sqlite3

from authentication import bootstrap_from_environment
from config import Config, ConfigurationError
from lidl_client import LidlClientError, safe_error_label
from session_store import SessionStateError


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--replace", action="store_true", help="Replace an existing session after a new normal login.")
    args = parser.parse_args()
    try:
        bootstrap_from_environment(Config.from_env(), replace=args.replace)
        print("Encrypted session saved. Healthy checks reuse tokens; credentials are used only for normal-login recovery.")
        return 0
    except ConfigurationError as error:
        print(f"Configuration error: {error}")
        return 1
    except SessionStateError as error:
        print(f"Session setup stopped: {error}")
        return 1
    except LidlClientError as error:
        print(f"Session setup stopped: {safe_error_label(error)}. Complete normal sign-in if Lidl requires a challenge.")
        return 1
    except (sqlite3.Error, OSError):
        print("Unable to save session state. Check the local file path and permissions.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
