"""Per-boot session tokens.

tftpjail mints one of these when it renders a machine's grub.cfg and injects it
into the kernel command line. The image's reporter reads it from /proc/cmdline
and presents it on every status POST. The server validates it here.

A token authenticates *this particular boot* of a known MAC — not a user, and
not a long-lived machine identity. It expires when the boot ends. This is
telemetry-grade authentication: it stops spoofed status reports, but it does
not grant any privilege at the OS layer.

Two storage modes:
  - ``BootSessionStore()`` keeps state in-process (used by tests).
  - ``BootSessionStore("/path/to/db.sqlite")`` persists to SQLite so server
    restarts don't invalidate every booted machine's token. The image's
    periodic heartbeat reporter relies on this — without persistence, after
    a fleetboot restart no machine could ever update its state again.
"""

from __future__ import annotations

import secrets
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from typing import Optional

from fleetboot.boot_states import BootState, state_index


# How many bytes of entropy in a token. 32 bytes -> 256 bits, hex-encoded to 64
# characters. Plenty for an unguessable per-boot secret on a LAN.
TOKEN_ENTROPY_BYTES = 32


_SESSIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS boot_sessions (
    token             TEXT PRIMARY KEY NOT NULL,
    mac               TEXT NOT NULL,
    minted_at         TEXT NOT NULL DEFAULT (datetime('now')),
    latest_state      TEXT,
    latest_state_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_boot_sessions_mac ON boot_sessions(mac);
"""


@dataclass
class BootSession:
    """One boot of one machine. Created by mint(), consumed by validate()."""

    token: str
    mac: str
    # The highest-ordered state we have seen reported on this session. None
    # means "no report yet". Used to reject out-of-order reports.
    latest_state: BootState | None = None
    # All reports received this process-lifetime, in arrival order. Useful for
    # in-process tests; not persisted. Persistent mode keeps only the latest
    # state — the registry's boot_events table is the durable log.
    reports: list[BootState] = field(default_factory=list)


class BootSessionStore:
    """Thread-safe store of active boot sessions.

    Pass a ``database_path`` to persist sessions across server restarts;
    omit it for an in-process dict (the test default).
    """

    def __init__(self, database_path: Path | str | None = None) -> None:
        self._lock = Lock()
        self._path = str(database_path) if database_path is not None else None
        if self._path is not None:
            # SQLite-backed: initialise the schema (idempotent).
            with self._connect() as connection:
                connection.executescript(_SESSIONS_SCHEMA)
                # WAL gives us safer concurrent reads, same as the registry.
                try:
                    connection.execute("PRAGMA journal_mode=WAL")
                except sqlite3.OperationalError:
                    pass
                # One-shot rename migrations: a freshly-deployed server
                # may inherit rows from before a state rename (e.g.
                # login_ready→login_console) or a dropped state
                # (user_logged_in). Convert the rows so the BootState
                # constructor doesn't choke on load.
                connection.execute(
                    "UPDATE boot_sessions SET latest_state = 'login_console' "
                    "WHERE latest_state = 'login_ready'"
                )
                connection.execute(
                    "UPDATE boot_sessions SET latest_state = 'login_console' "
                    "WHERE latest_state = 'user_logged_in'"
                )
            self._sessions: dict[str, BootSession] = {}  # unused in DB mode
        else:
            # In-memory: a plain dict guarded by the same lock.
            self._sessions = {}

    def _connect(self) -> sqlite3.Connection:
        assert self._path is not None
        connection = sqlite3.connect(self._path, isolation_level=None)
        connection.row_factory = sqlite3.Row
        return connection

    def mint(self, mac: str) -> BootSession:
        """Mint a fresh session token for a machine that is about to boot.

        Called by tftpjail at the moment it renders a grub.cfg for `mac`.
        """
        normalised_mac = _normalise_mac(mac)
        token = secrets.token_hex(TOKEN_ENTROPY_BYTES)
        session = BootSession(token=token, mac=normalised_mac)
        with self._lock:
            if self._path is not None:
                with self._connect() as connection:
                    connection.execute(
                        "INSERT INTO boot_sessions (token, mac) VALUES (?, ?)",
                        (token, normalised_mac),
                    )
            else:
                self._sessions[token] = session
        return session

    def lookup(self, token: str) -> BootSession | None:
        """Return the session for a token, or None if unknown."""
        if self._path is not None:
            return self._load_from_db(token)
        with self._lock:
            return self._sessions.get(token)

    def _load_from_db(self, token: str) -> BootSession | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT token, mac, latest_state FROM boot_sessions WHERE token = ?",
                (token,),
            ).fetchone()
        if row is None:
            return None
        latest_state = (
            BootState(row["latest_state"])
            if row["latest_state"] is not None
            else None
        )
        # `reports` is intentionally empty in persistent mode — the registry's
        # boot_events table is the durable log of every report.
        return BootSession(
            token=row["token"], mac=row["mac"], latest_state=latest_state,
        )

    def record_state(self, token: str, state: BootState) -> BootSession:
        """Record a reported state against a session.

        Raises UnknownTokenError if the token is not active, and
        OutOfOrderStateError if the report goes backwards. We accept the same
        state twice as a no-op (units restarting is benign, and the periodic
        heartbeat re-sends the current state every few minutes).
        """
        with self._lock:
            if self._path is not None:
                return self._record_in_db(token, state)
            return self._record_in_memory(token, state)

    def _record_in_memory(self, token: str, state: BootState) -> BootSession:
        session = self._sessions.get(token)
        if session is None:
            raise UnknownTokenError(token)
        # We used to reject out-of-order reports, but the lifecycle
        # isn't actually linear: scratch_mounted, network_up, and
        # login_console can fire in different orders depending on how
        # quickly the local disk, network, and display-manager
        # converge. Accept any valid state; latest_state still tracks
        # the highest-index "furthest along" reached.
        session.reports.append(state)
        if session.latest_state is None or state_index(state) > state_index(
            session.latest_state
        ):
            session.latest_state = state
        return session

    def _record_in_db(self, token: str, state: BootState) -> BootSession:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT mac, latest_state FROM boot_sessions WHERE token = ?",
                (token,),
            ).fetchone()
            if row is None:
                raise UnknownTokenError(token)
            previous = (
                BootState(row["latest_state"])
                if row["latest_state"] is not None
                else None
            )
            # Accept any valid state. The lifecycle isn't strictly
            # linear — scratch_mounted, network_up, login_console can
            # arrive in different orders depending on which subsystem
            # converges first. latest_state still tracks the highest-
            # index "furthest along" reached so the dashboard shows the
            # most-progressed state, and the timestamp always advances.
            if previous is None or state_index(state) >= state_index(previous):
                new_latest = state
            else:
                new_latest = previous
            connection.execute(
                "UPDATE boot_sessions "
                "SET latest_state = ?, latest_state_at = datetime('now') "
                "WHERE token = ?",
                (new_latest.value, token),
            )
        return BootSession(
            token=token, mac=row["mac"], latest_state=new_latest,
        )

    def end(self, token: str) -> None:
        """Drop a session — called when a machine reboots or is taken offline."""
        with self._lock:
            if self._path is not None:
                with self._connect() as connection:
                    connection.execute(
                        "DELETE FROM boot_sessions WHERE token = ?", (token,)
                    )
            else:
                self._sessions.pop(token, None)

    def last_seen_by_mac(self) -> dict[str, str]:
        """Return {mac: ISO-8601 last_state_at} for every machine that has
        ever reported. Picks the most recent session per MAC. Used by the
        dashboard to show "N min ago" and to flag stale rows.
        """
        result: dict[str, str] = {}
        if self._path is not None:
            with self._connect() as connection:
                rows = connection.execute(
                    "SELECT mac, MAX(latest_state_at) AS seen "
                    "FROM boot_sessions "
                    "WHERE latest_state_at IS NOT NULL "
                    "GROUP BY mac"
                ).fetchall()
            for row in rows:
                if row["seen"]:
                    result[row["mac"]] = row["seen"]
            return result
        # In-memory mode keeps no timestamps; tests that need this should
        # use persistent mode.
        return result

    def active_sessions(self) -> list[BootSession]:
        """Snapshot of all currently active sessions (for fleet views/tests)."""
        if self._path is not None:
            with self._connect() as connection:
                rows = connection.execute(
                    "SELECT token, mac, latest_state FROM boot_sessions"
                ).fetchall()
            return [
                BootSession(
                    token=r["token"],
                    mac=r["mac"],
                    latest_state=(
                        BootState(r["latest_state"])
                        if r["latest_state"] is not None
                        else None
                    ),
                )
                for r in rows
            ]
        with self._lock:
            return list(self._sessions.values())


class UnknownTokenError(Exception):
    """Raised when a status report presents a token we do not know."""

    def __init__(self, token: str) -> None:
        super().__init__("unknown boot session token")
        self.token = token


class OutOfOrderStateError(Exception):
    """Raised when a status report goes backwards in the lifecycle order."""

    def __init__(self, previous: BootState, attempted: BootState) -> None:
        super().__init__(
            f"cannot report {attempted.value} after {previous.value}"
        )
        self.previous = previous
        self.attempted = attempted


def _normalise_mac(mac: str) -> str:
    """Lowercase and colon-separate a MAC for consistent storage."""
    cleaned = mac.replace("-", ":").replace(".", ":").lower().strip()
    return cleaned
