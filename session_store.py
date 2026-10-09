"""Encrypted tokens and a durable, cross-process lease for token rotation."""

from contextlib import contextmanager
from dataclasses import dataclass, field
import errno
import json
import math
import os
from pathlib import Path
import sqlite3
import time
from typing import Callable, Iterator, Protocol, TYPE_CHECKING

from cryptography.fernet import Fernet, InvalidToken

from lidl_client import AuthenticationExpiredError

if TYPE_CHECKING:
    from config import Config


class SessionStateError(AuthenticationExpiredError):
    """Stop for a normal login when the stored session cannot be trusted."""


@dataclass(frozen=True, slots=True)
class SessionTokens:
    access_token: str = field(repr=False)
    refresh_token: str = field(repr=False)
    client_id: str = field(repr=False)
    client_secret: str = field(repr=False)
    expires_at: float

    def __post_init__(self) -> None:
        for value in (self.access_token, self.refresh_token, self.client_id, self.client_secret):
            if not isinstance(value, str) or not value or not value.isascii() or any(
                char.isspace() or ord(char) < 33 or ord(char) == 127 for char in value
            ):
                raise SessionStateError("Invalid authentication fields; sign in again.")
        if isinstance(self.expires_at, bool) or not isinstance(self.expires_at, (int, float)) or not math.isfinite(self.expires_at) or self.expires_at < 0:
            raise SessionStateError("Invalid token expiration; sign in again.")


class TokenCipher:
    def __init__(self, key: str) -> None:
        try:
            self._cipher = Fernet(key.encode("ascii"))
        except (ValueError, UnicodeError):
            raise SessionStateError("LIDL_TOKEN_STATE_KEY must be a valid Fernet key.") from None

    def encrypt(self, tokens: SessionTokens) -> bytes:
        payload = {name: getattr(tokens, name) for name in SessionTokens.__dataclass_fields__}
        return self._cipher.encrypt(json.dumps(payload, allow_nan=False).encode("utf-8"))

    def decrypt(self, ciphertext: bytes) -> SessionTokens:
        try:
            return SessionTokens(**json.loads(self._cipher.decrypt(ciphertext)))
        except (InvalidToken, ValueError, TypeError, UnicodeError):
            raise SessionStateError("Unable to decrypt the session; check the encryption key or sign in again.") from None


class RenewableSessionStore(Protocol):
    def access_token(self, renew: Callable[[SessionTokens], SessionTokens], *, rejected_token: str | None = None) -> str: ...
    def bootstrap(self, login: Callable[[], SessionTokens], *, replace: bool = False) -> None: ...


def create_session_store(config: "Config") -> RenewableSessionStore:
    if not config.token_state_key:
        raise SessionStateError("Set LIDL_TOKEN_STATE_KEY before bootstrapping a renewable session.")
    if config.state_backend == "postgres":
        from postgres_store import PostgresSessionStore
        return PostgresSessionStore(config.database_url, config.token_state_key, config.state_account_key, ca_cert=config.database_ca_cert, lock_timeout=config.http_timeout_seconds + 20)
    return SessionStore(config.token_state_path, config.token_state_key, lock_timeout=config.http_timeout_seconds + 20)


class SessionStore:
    def __init__(self, path: Path, key: str, *, lock_timeout: float = 40.0, clock: Callable[[], float] = time.time) -> None:
        self._codec = TokenCipher(key)
        self.path = Path(path)
        self._clock = clock
        self._lock_timeout = lock_timeout
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._locked() as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS session (id INTEGER PRIMARY KEY CHECK (id = 1), ciphertext BLOB NOT NULL, renewal_pending INTEGER NOT NULL CHECK (renewal_pending IN (0, 1)))")
            connection.commit()

    @contextmanager
    def _lease(self) -> Iterator[None]:
        # A stable OS lock file keeps the lease across SQLite commits. The OS
        # releases it on a crash; never delete/replace this file during a run.
        descriptor = os.open(str(self.path) + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
        acquired = False
        try:
            if os.fstat(descriptor).st_size == 0:
                os.write(descriptor, b"\0")
            deadline = time.monotonic() + self._lock_timeout
            while not acquired:
                try:
                    if os.name == "nt":
                        import msvcrt
                        os.lseek(descriptor, 0, os.SEEK_SET)
                        msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                except OSError as error:
                    if error.errno not in {errno.EACCES, errno.EAGAIN}:
                        raise SessionStateError("Unable to acquire the session file lock.") from None
                    if time.monotonic() >= deadline:
                        raise SessionStateError("Session renewal is busy; try a later check.") from None
                    time.sleep(0.05)
            yield
        finally:
            # Closing the descriptor also releases its OS lock.
            os.close(descriptor)

    @contextmanager
    def _locked(self) -> Iterator[sqlite3.Connection]:
        with self._lease():
            connection = sqlite3.connect(self.path, timeout=self._lock_timeout, isolation_level=None)
            try:
                connection.execute("PRAGMA synchronous=FULL")
                connection.execute("BEGIN IMMEDIATE")
                yield connection
            finally:
                connection.close()

    def _encode(self, tokens: SessionTokens) -> bytes:
        return self._codec.encrypt(tokens)

    def _decode(self, ciphertext: bytes) -> SessionTokens:
        return self._codec.decrypt(ciphertext)

    def save(self, tokens: SessionTokens, *, replace: bool = False) -> None:
        """Bootstrap only; replacement requires an explicit new login."""
        ciphertext = self._encode(tokens)
        with self._locked() as connection:
            if connection.execute("SELECT 1 FROM session WHERE id = 1").fetchone() and not replace:
                raise SessionStateError("A session already exists; use --replace after a new login.")
            connection.execute("INSERT OR REPLACE INTO session VALUES (1, ?, 0)", (ciphertext,))
            connection.commit()

    def bootstrap(self, login: Callable[[], SessionTokens], *, replace: bool = False) -> None:
        """Coordinate a new password login with ongoing token renewals."""
        with self._locked() as connection:
            exists = connection.execute("SELECT 1 FROM session WHERE id = 1").fetchone()
            if exists and not replace:
                raise SessionStateError("A session already exists; use --replace after a new login.")
            if exists:
                # A replacement login may invalidate the old pair even if its
                # response is lost. Do not silently resume using those tokens.
                connection.execute("UPDATE session SET renewal_pending = 1 WHERE id = 1")
                connection.commit()
            tokens = login()
            ciphertext = self._encode(tokens)
            if exists:
                connection.execute("BEGIN IMMEDIATE")
            connection.execute("INSERT OR REPLACE INTO session VALUES (1, ?, 0)", (ciphertext,))
            connection.commit()

    def access_token(self, renew: Callable[[SessionTokens], SessionTokens], *, rejected_token: str | None = None) -> str:
        with self._locked() as connection:
            row = connection.execute("SELECT ciphertext, renewal_pending FROM session WHERE id = 1").fetchone()
            if row is None:
                raise SessionStateError("No stored session. Run bootstrap_session.py once.")
            if row[1]:
                raise SessionStateError("Token renewal was interrupted; run bootstrap_session.py --replace to sign in again.")
            tokens = self._decode(row[0])
            if tokens.expires_at > self._clock() + 30 and (rejected_token is None or rejected_token != tokens.access_token):
                return tokens.access_token
            connection.execute("UPDATE session SET renewal_pending = 1 WHERE id = 1")
            connection.commit()
            # No retry after an ambiguous failure: the server may have rotated tokens.
            renewed = renew(tokens)
            if not isinstance(renewed, SessionTokens) or renewed.expires_at <= self._clock() + 30:
                raise SessionStateError("Token renewal returned an unusable session; sign in again.")
            ciphertext = self._encode(renewed)
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("UPDATE session SET ciphertext = ?, renewal_pending = 0 WHERE id = 1", (ciphertext,))
            connection.commit()
            return renewed.access_token
