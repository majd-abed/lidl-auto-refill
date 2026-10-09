"""Explicit one-time login. Passwords are never persisted or used by app.py."""

import argparse
import os
import sqlite3

import requests

from authentication import discover_client, request_tokens
from config import Config, ConfigurationError
from lidl_client import LidlClientError, safe_error_label
from session_store import SessionStateError, SessionStore


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--replace", action="store_true", help="Replace an existing session after a new normal login.")
    args = parser.parse_args()
    session = requests.Session()
    session.trust_env = False
    session.mount("https://", requests.adapters.HTTPAdapter(max_retries=0))
    try:
        config = Config.from_env()
        username, password = os.environ.get("LIDL_USERNAME"), os.environ.get("LIDL_PASSWORD")
        if not config.token_state_key or not username or not password:
            raise ConfigurationError("Set LIDL_TOKEN_STATE_KEY, LIDL_USERNAME, and LIDL_PASSWORD for the one-time login.")
        store = SessionStore(config.token_state_path, config.token_state_key, lock_timeout=config.http_timeout_seconds + 20)
        client_id, client_secret = os.environ.get("LIDL_CLIENT_ID"), os.environ.get("LIDL_CLIENT_SECRET")
        if bool(client_id) != bool(client_secret):
            raise ConfigurationError("Set both LIDL_CLIENT_ID and LIDL_CLIENT_SECRET, or leave both unset for portal discovery.")
        if not client_id:
            client_id, client_secret = discover_client(session, timeout=config.http_timeout_seconds)
        def login():
            return request_tokens(session, {
                "grant_type": "password", "client_id": client_id, "client_secret": client_secret,
                "username": username.replace("+", "").replace(" ", ""), "password": password,
                "captcha_code": "", "captcha_token": None,
            }, timeout=config.http_timeout_seconds, client_id=client_id, client_secret=client_secret, login=True)

        store.bootstrap(login, replace=args.replace)
        print("Encrypted session saved. Remove LIDL_USERNAME and LIDL_PASSWORD from the environment before running app.py.")
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
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
