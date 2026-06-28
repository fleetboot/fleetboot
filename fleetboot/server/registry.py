"""Persistent registry: which MAC has which profile, arch, platform.

The registry is the source of truth tftpjail consults via the
``/machines/{mac}`` API. We back it with SQLite because it keeps deployment
trivial (one file) while giving us atomic writes, an indexed lookup, and a
schema we can evolve. Throughput is not a concern at our scale — a school is
hundreds of machines, lookups happen at boot.

Concurrency: SQLite's WAL mode plus our short-lived connections gives us
safe concurrent reads with one writer at a time, which is the access pattern
the API has anyway (lookups dominate; enrolment is rare).
"""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


# Schema lives here so tests and migrations have one place to look. We use
# ``IF NOT EXISTS`` so the first call is the migration; for any later schema
# change we'll add an explicit migration step that runs alongside this.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS machines (
    mac             TEXT PRIMARY KEY NOT NULL,
    profile_name    TEXT NOT NULL,
    architecture    TEXT NOT NULL,
    platform        TEXT NOT NULL,
    serial_console  INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS boot_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    mac             TEXT NOT NULL,
    state           TEXT NOT NULL,
    detail          TEXT,
    occurred_at     TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_boot_events_mac_time
    ON boot_events(mac, occurred_at);
CREATE INDEX IF NOT EXISTS idx_boot_events_time
    ON boot_events(occurred_at);
"""


# Migration step: older databases didn't have the serial_console column.
# Add it idempotently — SQLite has no `IF NOT EXISTS` for ALTER TABLE so we
# catch the duplicate-column error and shrug it off.
def _add_serial_console_column_if_missing(connection: sqlite3.Connection) -> None:
    try:
        connection.execute(
            "ALTER TABLE machines ADD COLUMN serial_console INTEGER NOT NULL DEFAULT 0"
        )
    except sqlite3.OperationalError:
        pass


@dataclass(frozen=True)
class BootEvent:
    """One row from the boot_events log."""

    id: int
    mac: str
    state: str
    detail: Optional[str]
    occurred_at: str


@dataclass(frozen=True)
class Machine:
    """One row of the machines table — a registered MAC and its profile."""

    mac: str
    profile_name: str
    architecture: str
    platform: str
    created_at: str  # ISO-8601 UTC, stored as text
    # True when the kernel cmdline should include `console=ttyS0`. Set on VMs
    # and headless debug hardware; left False on student-facing desktops so
    # the OS doesn't burn cycles on a non-existent serial port.
    serial_console: bool = False


class MachineRegistry:
    """Thread-safe SQLite-backed machine registry.

    Pass ``database_path`` to persist to disk, or ``:memory:`` for tests.
    """

    def __init__(self, database_path: Path | str) -> None:
        self._path = str(database_path)
        # One write lock guards enrolment/deletion. Reads go straight to
        # SQLite — its own locking handles concurrent readers.
        self._write_lock = threading.Lock()
        with self._connect() as connection:
            connection.executescript(_SCHEMA)
            _add_serial_console_column_if_missing(connection)
            # WAL gives us safer concurrent reads without sacrificing
            # durability on writes. Harmless on in-memory databases.
            try:
                connection.execute("PRAGMA journal_mode=WAL")
            except sqlite3.OperationalError:
                pass

    def _connect(self) -> sqlite3.Connection:
        """Short-lived connection per call: simplest correct pattern here."""
        connection = sqlite3.connect(self._path, isolation_level=None)
        connection.row_factory = sqlite3.Row
        return connection

    # ---- CRUD -------------------------------------------------------------

    def enroll(
        self,
        *,
        mac: str,
        profile_name: str,
        architecture: str,
        platform: str,
        serial_console: bool = False,
    ) -> Machine:
        """Insert (or replace) a machine. Returns the canonical row."""
        normalised_mac = _normalise_mac(mac)
        with self._write_lock, self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO machines "
                "(mac, profile_name, architecture, platform, serial_console) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    normalised_mac, profile_name, architecture,
                    platform, 1 if serial_console else 0,
                ),
            )
        result = self.lookup(normalised_mac)
        # lookup must succeed -- we just wrote the row inside the lock.
        assert result is not None
        return result

    def lookup(self, mac: str) -> Optional[Machine]:
        """Return the registered Machine for ``mac``, or None."""
        normalised_mac = _normalise_mac(mac)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT mac, profile_name, architecture, platform, "
                "       serial_console, created_at "
                "FROM machines WHERE mac = ?",
                (normalised_mac,),
            ).fetchone()
        if row is None:
            return None
        return _row_to_machine(row)

    def list_all(self) -> list[Machine]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT mac, profile_name, architecture, platform, "
                "       serial_console, created_at "
                "FROM machines ORDER BY created_at, mac"
            ).fetchall()
        return [_row_to_machine(r) for r in rows]

    # ---- boot events ------------------------------------------------------

    def log_boot_event(
        self,
        *,
        mac: str,
        state: str,
        detail: Optional[str] = None,
    ) -> None:
        """Append-only log of state transitions reported by /status."""
        normalised_mac = _normalise_mac(mac)
        with self._write_lock, self._connect() as connection:
            connection.execute(
                "INSERT INTO boot_events (mac, state, detail) "
                "VALUES (?, ?, ?)",
                (normalised_mac, state, detail),
            )

    def recent_boot_events(
        self, *, limit: int = 200, mac: Optional[str] = None,
    ) -> list[BootEvent]:
        """Return the latest `limit` events, newest first.

        If `mac` is provided, filter to that MAC's events only.
        """
        with self._connect() as connection:
            if mac is not None:
                rows = connection.execute(
                    "SELECT id, mac, state, detail, occurred_at "
                    "FROM boot_events WHERE mac = ? "
                    "ORDER BY occurred_at DESC, id DESC LIMIT ?",
                    (_normalise_mac(mac), limit),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT id, mac, state, detail, occurred_at "
                    "FROM boot_events "
                    "ORDER BY occurred_at DESC, id DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [
            BootEvent(
                id=row["id"],
                mac=row["mac"],
                state=row["state"],
                detail=row["detail"],
                occurred_at=row["occurred_at"],
            )
            for row in rows
        ]

    def remove(self, mac: str) -> bool:
        """Delete a machine. Returns True if a row was removed."""
        normalised_mac = _normalise_mac(mac)
        with self._write_lock, self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM machines WHERE mac = ?", (normalised_mac,)
            )
            return cursor.rowcount > 0


# ---- Helpers --------------------------------------------------------------


def _normalise_mac(mac: str) -> str:
    """Same normalisation rule as BootSessionStore — lowercase colon form."""
    return mac.replace("-", ":").replace(".", ":").lower().strip()


def _row_to_machine(row: sqlite3.Row) -> Machine:
    return Machine(
        mac=row["mac"],
        profile_name=row["profile_name"],
        architecture=row["architecture"],
        platform=row["platform"],
        serial_console=bool(row["serial_console"]),
        created_at=row["created_at"],
    )
