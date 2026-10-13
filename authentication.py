"""The password and refresh grants observed in Lidl's public portal."""

from html.parser import HTMLParser
import json
import math
import os
import re
import time
from urllib.parse import urljoin, urlparse

import requests

from config import Config, ConfigurationError

from lidl_client import (
    AuthenticationExpiredError, RequestTimeoutError, ServiceUnavailableError,
    UnexpectedHTTPStatusError, UnexpectedResponseError,
)
from session_store import SessionTokens, create_session_store


TOKEN_URL = "https://api.lidl-connect.de/api/token"
PORTAL_URL = "https://kundenkonto.lidl-connect.de/"


def bootstrap_from_environment(config: Config, *, replace: bool = False) -> None:
    """One normal password grant, coordinated with renewals and supplied via Secrets."""
    username, password = os.environ.get("LIDL_USERNAME"), os.environ.get("LIDL_PASSWORD")
    if not config.token_state_key or not username or not password:
        raise ConfigurationError("Set LIDL_TOKEN_STATE_KEY, LIDL_USERNAME, and LIDL_PASSWORD for a normal login.")
    client_id, client_secret = os.environ.get("LIDL_CLIENT_ID"), os.environ.get("LIDL_CLIENT_SECRET")
    if bool(client_id) != bool(client_secret):
        raise ConfigurationError("Set both LIDL_CLIENT_ID and LIDL_CLIENT_SECRET, or leave both unset for portal discovery.")
    store = create_session_store(config)
    with requests.Session() as session:
        session.trust_env = False
        session.mount("https://", requests.adapters.HTTPAdapter(max_retries=0))
        if not client_id:
            client_id, client_secret = discover_client(session, timeout=config.http_timeout_seconds)
        def login() -> SessionTokens:
            return request_tokens(session, {
                "grant_type": "password", "client_id": client_id, "client_secret": client_secret,
                "username": username.replace("+", "").replace(" ", ""), "password": password,
                "captcha_code": "", "captcha_token": None,
            }, timeout=config.http_timeout_seconds, client_id=client_id, client_secret=client_secret, login=True)
        store.bootstrap(login, replace=replace)


def request_tokens(session: requests.Session, payload: dict, *, timeout: float, client_id: str, client_secret: str, login: bool = False) -> SessionTokens:
    try:
        response = session.post(TOKEN_URL, json=payload, timeout=timeout, allow_redirects=False)
    except requests.Timeout:
        raise RequestTimeoutError("Token request timed out; sign in again if renewal was interrupted.") from None
    except requests.RequestException:
        raise ServiceUnavailableError("Unable to complete the token request.") from None
    try:
        if response.status_code in {400, 401, 403}:
            raise AuthenticationExpiredError("Sign in normally if Lidl requires a CAPTCHA, SMS code, or a new login.", response.status_code)
        if response.status_code != 200:
            raise UnexpectedHTTPStatusError(response.status_code)
        try:
            data = response.json()
        except ValueError:
            raise UnexpectedResponseError("Invalid authentication response.") from None
        if not isinstance(data, dict) or not isinstance(data.get("token_type"), str) or data["token_type"].lower() != "bearer":
            raise UnexpectedResponseError("Unrecognized authentication response.")
        challenge = data.get("needs_multifactor_authentication")
        if (login and challenge is not False) or (not login and challenge is not None and challenge is not False):
            raise AuthenticationExpiredError("Complete Lidl's normal sign-in challenge before bootstrapping a session.")
        lifetime = data.get("expires_in")
        if isinstance(lifetime, bool) or not isinstance(lifetime, (int, float)) or not math.isfinite(lifetime) or lifetime <= 30:
            raise UnexpectedResponseError("Invalid token lifetime.")
        return SessionTokens(data.get("access_token"), data.get("refresh_token"), client_id, client_secret, time.time() + lifetime)
    finally:
        response.close()


class _Scripts(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.sources: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "script":
            source = dict(attrs).get("src")
            if source:
                self.sources.append(source)


def discover_client(session: requests.Session, *, timeout: float) -> tuple[str, str]:
    """Read published client settings from the current portal bundle; never execute it."""
    def read(url: str) -> str:
        try:
            response = session.get(url, timeout=timeout, allow_redirects=False)
        except requests.Timeout:
            raise RequestTimeoutError("Portal settings request timed out.") from None
        except requests.RequestException:
            raise ServiceUnavailableError("Unable to read portal settings.") from None
        try:
            # The observed root redirects to this same-host landing page.
            if response.status_code in {301, 302, 303, 307, 308}:
                destination = urljoin(url, response.headers.get("Location", ""))
                if url != PORTAL_URL or destination != urljoin(PORTAL_URL, "mein-lidl-connect.html"):
                    raise UnexpectedResponseError("The portal landing page changed; inspect it before proceeding.")
                return read(destination)
            if response.status_code != 200:
                raise UnexpectedHTTPStatusError(response.status_code)
            if len(response.content) > 8 * 1024 * 1024:
                raise UnexpectedResponseError("Unexpected portal settings size.")
            return response.text
        finally:
            response.close()

    parser = _Scripts()
    parser.feed(read(PORTAL_URL))
    candidates = []
    for source in parser.sources:
        url = urljoin(PORTAL_URL, source)
        parts = urlparse(url)
        if parts.scheme == "https" and parts.netloc == urlparse(PORTAL_URL).netloc and parts.path == "/assets/brfrontend/dist/production/js/lidl.js":
            candidates.append(url)
    if len(candidates) != 1:
        raise UnexpectedResponseError("Cannot identify the observed Lidl portal bundle.")
    quoted = r'("(?:[^"\\]|\\.)*")'
    matches = re.findall(r'\.commit\("selectClient",\{id:' + quoted + r',secret:' + quoted + r'\}\)', read(candidates[0]))
    if len(matches) != 1:
        raise UnexpectedResponseError("The portal client configuration changed; inspect it before proceeding.")
    return json.loads(matches[0][0]), json.loads(matches[0][1])
