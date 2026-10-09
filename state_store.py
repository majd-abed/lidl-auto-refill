"""Durable, atomic refill claims for processes sharing one local SQLite file."""

from contextlib import contextmanager
from dataclasses import dataclass
import math
from pathlib import Path
import sqlite3
from typing import Iterator
from uuid import uuid4


class StateStoreError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class RefillRecord:
    request_id: str
    requested_at: float
    before_gb: float
    status: str


@dataclass(frozen=True, slots=True)
class ClaimResult:
    record: RefillRecord | None
    blocked_by: str | None = None


class SQLiteStateStore:
    """Use one file per account. Never delete it to work around a pending request."""

    def __init__(self, path: Path) -> None:
        self.path = path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with self._connection() as connection:
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS refill_state ("
                    "id INTEGER PRIMARY KEY CHECK (id = 1), "
                    "request_id TEXT NOT NULL, requested_at REAL NOT NULL, "
                    "before_gb REAL NOT NULL, status TEXT NOT NULL "
                    "CHECK (status IN ('pending', 'confirmed')))"
                )
        except OSError:
            raise StateStoreError("Cannot create the refill state directory.") from None

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = None
        try:
            connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
            yield connection
        except sqlite3.Error:
            raise StateStoreError("Cannot safely read or write the refill state database.") from None
        finally:
            if connection is not None:
                connection.close()

    @staticmethod
    def _read(connection: sqlite3.Connection) -> RefillRecord | None:
        row = connection.execute(
            "SELECT request_id, requested_at, before_gb, status FROM refill_state WHERE id = 1"
        ).fetchone()
        if row is None:
            return None
        request_id, requested_at, before_gb, status = row
        if (
            not isinstance(request_id, str) or not request_id
            or not isinstance(requested_at, (int, float)) or not math.isfinite(requested_at)
            or requested_at < 0
            or not isinstance(before_gb, (int, float)) or not math.isfinite(before_gb)
            or before_gb < 0 or status not in {"pending", "confirmed"}
        ):
            raise StateStoreError("Refill state is invalid; refusing to request a refill.")
        return RefillRecord(request_id, requested_at, before_gb, status)

    def get(self) -> RefillRecord | None:
        with self._connection() as connection:
            return self._read(connection)

    def claim(self, before_gb: float, now: float, cooldown_seconds: int) -> ClaimResult:
        if cooldown_seconds < 600:
            raise StateStoreError("Refill cooldown must be at least 600 seconds.")
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            previous = self._read(connection)
            if previous is not None:
                if previous.status == "pending":
                    return ClaimResult(None, "pending")
                if now < previous.requested_at + cooldown_seconds:
                    return ClaimResult(None, "cooldown")
            record = RefillRecord(str(uuid4()), now, before_gb, "pending")
            connection.execute(
                "INSERT INTO refill_state (id, request_id, requested_at, before_gb, status) "
                "VALUES (1, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET "
                "request_id=excluded.request_id, requested_at=excluded.requested_at, "
                "before_gb=excluded.before_gb, status=excluded.status",
                (record.request_id, record.requested_at, record.before_gb, record.status),
            )
            # Persist intent before returning permission to send the request.
            connection.execute("COMMIT")
            return ClaimResult(record)

    def confirm(self, request_id: str) -> None:
        with self._connection() as connection:
            result = connection.execute(
                "UPDATE refill_state SET status = 'confirmed' "
                "WHERE id = 1 AND request_id = ?",
                (request_id,),
            )
            if result.rowcount != 1:
                raise StateStoreError("Refill state changed during verification; refusing further actions.")
