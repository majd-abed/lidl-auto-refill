"""Durable cloud state. Atomic SQL claims survive runner loss and overlapping jobs."""

from contextlib import contextmanager, ExitStack
import math
import tempfile
import time
from typing import Callable, Iterator
from uuid import uuid4

import psycopg
from psycopg.conninfo import conninfo_to_dict

from session_store import SessionStateError, SessionTokens, TokenCipher
from state_store import ClaimResult, RefillRecord, StateStoreError


def _cloud_error_message(error: psycopg.Error) -> str:
    # Inspect private driver diagnostics, but expose only fixed recovery messages.
    detail = str(error).lower()
    if error.sqlstate in {"28000", "28P01"} or any(text in detail for text in ("password authentication failed", "tenant or user not found")):
        return "Cloud state authentication failed. Check the database username and password in DATABASE_URL."
    if any(text in detail for text in ("certificate verify failed", "root certificate file", "could not read root certificate")):
        return "Cloud state TLS certificate verification failed. Set DATABASE_CA_CERT to the trusted Supabase root certificate."
    if any(text in detail for text in ("could not translate host name", "name or service not known", "nodename nor servname")):
        return "Cloud database hostname could not be resolved. Copy the complete Session pooler URI into DATABASE_URL."
    if any(text in detail for text in ("timeout expired", "connection timed out")):
        return "Cloud database connection timed out. Check Supabase project status, pooler port, and network access."
    if error.sqlstate == "42501" or "permission denied" in detail:
        return "Cloud state permissions are insufficient. Use the database owner login with access to the private schema."
    return "Cannot safely access cloud state. Check database connectivity, permissions, and TLS certificate settings."


class _Postgres:
    _error = StateStoreError

    def __init__(self, database_url: str, account_key: str, ca_cert: str | None = None) -> None:
        self._url = database_url
        self._account = account_key
        self._ca_cert = ca_cert

    @contextmanager
    def _database(self) -> Iterator[psycopg.Connection]:
        try:
            settings = conninfo_to_dict(self._url)
            local_test = settings.get("host") in {"localhost", "127.0.0.1", "::1"} and settings.get("sslmode") == "disable"
            with ExitStack() as stack:
                root_cert = "system"
                if self._ca_cert:
                    certificate = stack.enter_context(tempfile.NamedTemporaryFile(mode="w", encoding="ascii", suffix=".crt", delete_on_close=False))
                    certificate.write(self._ca_cert)
                    certificate.close()
                    root_cert = certificate.name
                connection = psycopg.connect(
                    self._url, autocommit=True, prepare_threshold=None,
                    connect_timeout=20, sslmode="disable" if local_test else "verify-full",
                    sslrootcert="" if local_test else root_cert,
                    keepalives=1, keepalives_idle=20, keepalives_interval=5, keepalives_count=2,
                    tcp_user_timeout=30000,
                )
                try:
                    yield connection
                finally:
                    connection.close()
        except psycopg.Error as error:
            raise self._error(_cloud_error_message(error)) from None
        except (ValueError, OSError):
            raise self._error("Cannot safely access cloud state. Check database connectivity, permissions, and TLS certificate settings.") from None

    @staticmethod
    def _write(connection: psycopg.Connection, query: str, parameters: tuple) -> int:
        with connection.transaction():
            connection.execute("SET LOCAL synchronous_commit = on")
            connection.execute("SET LOCAL statement_timeout = '20s'")
            connection.execute("SET LOCAL lock_timeout = '10s'")
            count = connection.execute(query, parameters).rowcount
        # Returning from the transaction confirms the claim was committed.
        return count

    @staticmethod
    def _read(connection: psycopg.Connection, query: str, parameters: tuple) -> tuple | None:
        with connection.transaction():
            connection.execute("SET LOCAL statement_timeout = '20s'")
            return connection.execute(query, parameters).fetchone()

    def _initialize(self) -> None:
        with self._database() as connection, connection.transaction():
            connection.execute("SET LOCAL statement_timeout = '20s'")
            connection.execute("SET LOCAL lock_timeout = '10s'")
            # Serialize schema/RLS setup to avoid concurrent DDL lock upgrades.
            # Transaction-scoped locks also work through transaction poolers.
            connection.execute("SELECT pg_advisory_xact_lock(1818846316, 0)")
            connection.execute("CREATE SCHEMA IF NOT EXISTS lidl_automation")
            connection.execute("REVOKE ALL ON SCHEMA lidl_automation FROM PUBLIC")
            connection.execute("CREATE TABLE IF NOT EXISTS lidl_automation.refills (account_key TEXT PRIMARY KEY, request_id TEXT NOT NULL, requested_at DOUBLE PRECISION NOT NULL, before_gb DOUBLE PRECISION NOT NULL, status TEXT NOT NULL CHECK (status IN ('pending', 'confirmed')))")
            connection.execute("CREATE TABLE IF NOT EXISTS lidl_automation.sessions (account_key TEXT PRIMARY KEY, ciphertext BYTEA, renewal_id TEXT, renewal_started_at DOUBLE PRECISION, CHECK (ciphertext IS NOT NULL OR renewal_id IS NOT NULL), CHECK ((renewal_id IS NULL) = (renewal_started_at IS NULL)))")
            # Private schema plus RLS without public policies keeps Supabase's
            # anonymous/authenticated API roles away from coordination state.
            connection.execute("ALTER TABLE lidl_automation.refills ENABLE ROW LEVEL SECURITY")
            connection.execute("ALTER TABLE lidl_automation.sessions ENABLE ROW LEVEL SECURITY")


class PostgresStateStore(_Postgres):
    def __init__(self, database_url: str, account_key: str = "default", *, ca_cert: str | None = None) -> None:
        super().__init__(database_url, account_key, ca_cert)
        self._initialize()

    def get(self) -> RefillRecord | None:
        with self._database() as connection:
            row = self._read(connection, "SELECT request_id, requested_at, before_gb, status FROM lidl_automation.refills WHERE account_key = %s", (self._account,))
        if row is None:
            return None
        record = RefillRecord(*row)
        if not record.request_id or record.status not in {"pending", "confirmed"} or any(not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0 for value in (record.requested_at, record.before_gb)):
            raise StateStoreError("Cloud refill state is invalid; refusing to request a refill.")
        return record

    def claim(self, before_gb: float, now: float, cooldown_seconds: int) -> ClaimResult:
        if cooldown_seconds < 600 or any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0 for value in (before_gb, now)):
            raise StateStoreError("Invalid cloud refill claim or cooldown.")
        record = RefillRecord(str(uuid4()), now, before_gb, "pending")
        with self._database() as connection:
            count = self._write(connection,
                "INSERT INTO lidl_automation.refills VALUES (%s, %s, %s, %s, 'pending') ON CONFLICT (account_key) DO UPDATE SET request_id = EXCLUDED.request_id, requested_at = EXCLUDED.requested_at, before_gb = EXCLUDED.before_gb, status = 'pending' WHERE refills.status = 'confirmed' AND refills.requested_at <= %s",
                (self._account, record.request_id, now, before_gb, now - cooldown_seconds),
            )
        if count == 1:
            return ClaimResult(record)
        previous = self.get()
        if previous is None:
            raise StateStoreError("Cloud refill state changed unexpectedly; no refill requested.")
        return ClaimResult(None, "pending" if previous.status == "pending" else "cooldown")

    def confirm(self, request_id: str) -> None:
        with self._database() as connection:
            count = self._write(connection, "UPDATE lidl_automation.refills SET status = 'confirmed' WHERE account_key = %s AND request_id = %s", (self._account, request_id))
        if count != 1:
            raise StateStoreError("Cloud refill state changed during verification; refusing further actions.")


class PostgresSessionStore(_Postgres):
    _error = SessionStateError

    def __init__(self, database_url: str, key: str, account_key: str = "default", *, ca_cert: str | None = None, lock_timeout: float = 40.0, clock: Callable[[], float] = time.time) -> None:
        super().__init__(database_url, account_key, ca_cert)
        self._codec = TokenCipher(key)
        self._clock = clock
        self._lock_timeout = lock_timeout
        self._initialize()

    def _row(self, connection: psycopg.Connection) -> tuple | None:
        return self._read(connection, "SELECT ciphertext, renewal_id, renewal_started_at FROM lidl_automation.sessions WHERE account_key = %s", (self._account,))

    def _complete(self, connection: psycopg.Connection, renewal_id: str, tokens: SessionTokens) -> None:
        if not isinstance(tokens, SessionTokens) or tokens.expires_at <= self._clock() + 30:
            raise SessionStateError("Authentication returned an unusable session; sign in again.")
        count = self._write(connection, "UPDATE lidl_automation.sessions SET ciphertext = %s, renewal_id = NULL, renewal_started_at = NULL WHERE account_key = %s AND renewal_id = %s", (self._codec.encrypt(tokens), self._account, renewal_id))
        if count != 1:
            raise SessionStateError("Cloud session changed during authentication; sign in again.")

    def bootstrap(self, login: Callable[[], SessionTokens], *, replace: bool = False) -> None:
        with self._database() as connection:
            row = self._row(connection)
            if row is not None and not replace:
                raise SessionStateError("A cloud session already exists; use --replace for a new login.")
            renewal_id = str(uuid4())
            if row is None:
                count = self._write(connection, "INSERT INTO lidl_automation.sessions VALUES (%s, NULL, %s, %s) ON CONFLICT (account_key) DO NOTHING", (self._account, renewal_id, self._clock()))
            else:
                count = self._write(connection, "UPDATE lidl_automation.sessions SET renewal_id = %s, renewal_started_at = %s WHERE account_key = %s AND (renewal_id IS NULL OR renewal_started_at <= %s)", (renewal_id, self._clock(), self._account, self._clock() - self._lock_timeout))
            if count != 1:
                raise SessionStateError("Cloud authentication is busy; wait for the active run before replacing it.")
            # Claim committed before password login, even for an empty session.
            self._complete(connection, renewal_id, login())

    def save(self, tokens: SessionTokens, *, replace: bool = False) -> None:
        self.bootstrap(lambda: tokens, replace=replace)

    def access_token(self, renew: Callable[[SessionTokens], SessionTokens], *, rejected_token: str | None = None) -> str:
        deadline = time.monotonic() + self._lock_timeout
        with self._database() as connection:
            while True:
                row = self._row(connection)
                if row is None:
                    raise SessionStateError("No stored cloud session. Run bootstrap_session.py once.")
                ciphertext, renewal_id, started_at = row
                if renewal_id is not None:
                    if not isinstance(started_at, (int, float)) or not math.isfinite(started_at) or self._clock() >= started_at + self._lock_timeout or time.monotonic() >= deadline:
                        raise SessionStateError("Cloud renewal is interrupted or still busy; use bootstrap_session.py --replace after the active run ends.")
                    time.sleep(0.1)
                    continue
                tokens = self._codec.decrypt(ciphertext)
                if tokens.expires_at > self._clock() + 30 and (rejected_token is None or rejected_token != tokens.access_token):
                    return tokens.access_token
                renewal_id = str(uuid4())
                count = self._write(connection, "UPDATE lidl_automation.sessions SET renewal_id = %s, renewal_started_at = %s WHERE account_key = %s AND renewal_id IS NULL AND ciphertext = %s", (renewal_id, self._clock(), self._account, ciphertext))
                if count == 0:
                    continue
                # A durable compare-and-swap claim protects rotation across
                # machines and commits, without session-level advisory locks.
                renewed = renew(tokens)
                self._complete(connection, renewal_id, renewed)
                return renewed.access_token
