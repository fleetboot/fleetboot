"""Persistent registry: which MAC has which profile, arch, platform.

The registry is the source of truth tftpjail consults via the
``/machines/{mac}`` API. We back it with SQLite because it keeps deployment
trivial (one file) while giving us atomic writes, an indexed lookup, and a
schema we can evolve. Throughput is not a concern at our target scale —
typical fleets are hundreds of machines and lookups happen at boot.

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
    enrolled_by     TEXT NOT NULL DEFAULT 'manual',
    hostname        TEXT,
    hostname_seen_at TEXT,
    boot_version    TEXT,
    boot_version_seen_at TEXT,
    scratch_mode    TEXT NOT NULL DEFAULT 'volatile'
                    CHECK(scratch_mode IN ('volatile', 'persistent', 'off')),
    last_diagnostics TEXT,
    last_diagnostics_at TEXT,
    last_hardware    TEXT,
    last_hardware_at TEXT,
    last_ip          TEXT,
    last_ip_at       TEXT,
    reboot_command   TEXT,
    -- Per-machine power control, symmetric with reboot_command: shell
    -- commands that switch this machine on and off. Left NULL on the
    -- normal case, where power is routed through the fleet-wide
    -- pdudaemon instead (see pdu_alias below).
    power_on_command  TEXT,
    power_off_command TEXT,
    -- The name this machine answers to on pdudaemon. Defaults to the
    -- reported hostname when NULL, but a machine that has never booted
    -- has no hostname — and powering *on* a machine that has never
    -- booted is exactly the on-demand case — so it can be set ahead of
    -- time.
    pdu_alias        TEXT,
    -- Soft-reboot signal. When non-zero, the next /status reply tells
    -- the machine to `systemctl reboot`. Used as a fallback when the
    -- PDU power-cycle command fails (or no PDU is configured): the
    -- machine reboots itself on its next heartbeat.
    pending_reboot   INTEGER NOT NULL DEFAULT 0,
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

-- Simple key-value store for admin-set fleet-wide settings (e.g. the
-- pdudaemon hostname used to assemble per-machine reboot URLs). Keeping
-- it as a flat KV table avoids a schema change every time we add a
-- single configurable; the keys themselves are documented at the call
-- sites that read them.
CREATE TABLE IF NOT EXISTS settings (
    key             TEXT PRIMARY KEY NOT NULL,
    value           TEXT NOT NULL,
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS auto_enrol_rules (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    match_kind      TEXT NOT NULL CHECK(match_kind IN ('mac_prefix', 'ip_cidr')),
    match_value     TEXT NOT NULL,
    profile_name    TEXT NOT NULL,
    architecture    TEXT NOT NULL DEFAULT 'x86_64',
    platform        TEXT NOT NULL DEFAULT 'any',
    serial_console  INTEGER NOT NULL DEFAULT 0,
    scratch_mode    TEXT NOT NULL DEFAULT 'volatile'
                    CHECK(scratch_mode IN ('volatile', 'persistent', 'off')),
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
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


def _add_enrolled_by_column_if_missing(connection: sqlite3.Connection) -> None:
    """Migration: older databases used 'manual' for everyone implicitly."""
    try:
        connection.execute(
            "ALTER TABLE machines ADD COLUMN enrolled_by TEXT NOT NULL DEFAULT 'manual'"
        )
    except sqlite3.OperationalError:
        pass


def _add_hostname_columns_if_missing(connection: sqlite3.Connection) -> None:
    """Migration: hostname tracking arrived after the initial schema."""
    for column_def in (
        "ALTER TABLE machines ADD COLUMN hostname TEXT",
        "ALTER TABLE machines ADD COLUMN hostname_seen_at TEXT",
    ):
        try:
            connection.execute(column_def)
        except sqlite3.OperationalError:
            pass


def _add_boot_version_columns_if_missing(connection: sqlite3.Connection) -> None:
    """Migration: per-boot build version tracking arrived later still."""
    for column_def in (
        "ALTER TABLE machines ADD COLUMN boot_version TEXT",
        "ALTER TABLE machines ADD COLUMN boot_version_seen_at TEXT",
    ):
        try:
            connection.execute(column_def)
        except sqlite3.OperationalError:
            pass


def _add_scratch_mode_columns_if_missing(
    connection: sqlite3.Connection,
) -> None:
    """Migration: local-disk scratch behaviour, per machine + per rule."""
    for table in ("machines", "auto_enrol_rules"):
        try:
            connection.execute(
                f"ALTER TABLE {table} "
                "ADD COLUMN scratch_mode TEXT NOT NULL DEFAULT 'volatile'"
            )
        except sqlite3.OperationalError:
            pass


def _add_diagnostics_columns_if_missing(
    connection: sqlite3.Connection,
) -> None:
    """Migration: latest reporter diagnostics live on the machine row."""
    for column_def in (
        "ALTER TABLE machines ADD COLUMN last_diagnostics TEXT",
        "ALTER TABLE machines ADD COLUMN last_diagnostics_at TEXT",
        "ALTER TABLE machines ADD COLUMN last_hardware TEXT",
        "ALTER TABLE machines ADD COLUMN last_hardware_at TEXT",
        "ALTER TABLE machines ADD COLUMN last_ip TEXT",
        "ALTER TABLE machines ADD COLUMN last_ip_at TEXT",
        "ALTER TABLE machines ADD COLUMN reboot_command TEXT",
        "ALTER TABLE machines ADD COLUMN pending_reboot INTEGER NOT NULL DEFAULT 0",
    ):
        try:
            connection.execute(column_def)
        except sqlite3.OperationalError:
            pass


def _add_power_columns_if_missing(connection: sqlite3.Connection) -> None:
    """Migration: on/off power control alongside the existing reboot."""
    for column_def in (
        "ALTER TABLE machines ADD COLUMN power_on_command TEXT",
        "ALTER TABLE machines ADD COLUMN power_off_command TEXT",
        "ALTER TABLE machines ADD COLUMN pdu_alias TEXT",
    ):
        try:
            connection.execute(column_def)
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
    # How this row got here: 'manual' for admin-entered, or 'rule:<name>'
    # when auto-enrol fired. Auditable provenance.
    enrolled_by: str = "manual"
    # Most recently reported hostname from the booted image, plus when.
    # Useful for human-readable lookups in the dashboard.
    hostname: Optional[str] = None
    hostname_seen_at: Optional[str] = None
    # The image build version string the machine is currently running,
    # written into the image at build time. The dashboard compares this
    # against the sidecar in build/ to colour rows green (up to date) or
    # orange (stale; needs reboot to pick up the new image).
    boot_version: Optional[str] = None
    boot_version_seen_at: Optional[str] = None
    # How the image should treat any local disk it finds:
    #   - volatile:   wipe + format ext4 every boot, mount /var/scratch
    #   - persistent: keep ext4 across boots (browser cache survives)
    #   - off:        ignore the disk entirely
    scratch_mode: str = "volatile"
    # Latest reporter diagnostics — overwritten by each non-empty
    # `diagnostics` field on /status. Used by the machine detail page
    # to answer "why is this machine stuck?".
    last_diagnostics: Optional[str] = None
    last_diagnostics_at: Optional[str] = None
    # Latest hardware inventory (CPU, RAM, disks) — JSON blob from the
    # reporter, rendered as a table on the machine detail page.
    last_hardware: Optional[str] = None
    last_hardware_at: Optional[str] = None
    # Last IP the machine was seen reporting from (server-side observed,
    # not client-claimed). Updated on every /status post.
    last_ip: Optional[str] = None
    last_ip_at: Optional[str] = None
    # Per-machine shell command run on the fleetboot host when an admin
    # clicks "delete + reboot". For now a free-text string with
    # shell=True semantics — this is dev-only power control; a real
    # structured-power-control layer is a future task.
    reboot_command: Optional[str] = None
    # Same idea for switching the machine on and off. Usually left
    # unset: most fleets route power through the fleet-wide pdudaemon,
    # which needs no per-machine command at all.
    power_on_command: Optional[str] = None
    power_off_command: Optional[str] = None
    # What this machine is called on pdudaemon. Falls back to the
    # reported hostname when unset.
    pdu_alias: Optional[str] = None
    # When True, the next /status response tells the machine to
    # `systemctl reboot`. Set as a soft fallback when an explicit
    # PDU power-cycle has failed (or no PDU is configured); cleared
    # implicitly when the machine re-enrols on its next PXE.
    pending_reboot: bool = False


@dataclass(frozen=True)
class AutoEnrolRule:
    """A rule that auto-creates a `machines` row when an unknown MAC asks
    for its config and matches the rule's predicate.

    Two predicate kinds:
      - `mac_prefix`: matches if the candidate MAC starts with `match_value`
        (lowercase, colon-separated, no wildcards). Handy for vendor OUIs.
      - `ip_cidr`: matches if the candidate's source IP falls within the
        CIDR (e.g. `192.168.99.0/24`). Handy for DHCP-segregated networks.
    """

    id: int
    name: str
    match_kind: str   # 'mac_prefix' or 'ip_cidr'
    match_value: str
    profile_name: str
    architecture: str
    platform: str
    serial_console: bool
    created_at: str
    scratch_mode: str = "volatile"


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
            _add_enrolled_by_column_if_missing(connection)
            _add_hostname_columns_if_missing(connection)
            _add_boot_version_columns_if_missing(connection)
            _add_scratch_mode_columns_if_missing(connection)
            _add_diagnostics_columns_if_missing(connection)
            _add_power_columns_if_missing(connection)
            # Rename / drop boot_events.state values that no longer
            # exist (e.g. login_ready → login_console; user_logged_in
            # was dropped entirely). Idempotent — no-op on a fresh DB.
            connection.execute(
                "UPDATE boot_events SET state = 'login_console' "
                "WHERE state = 'login_ready'"
            )
            connection.execute(
                "DELETE FROM boot_events WHERE state = 'user_logged_in'"
            )
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
        enrolled_by: str = "manual",
        scratch_mode: str = "volatile",
    ) -> Machine:
        """Insert (or replace) a machine. Returns the canonical row.

        Manual admin enrolments leave `enrolled_by='manual'`; the auto-enrol
        path passes `enrolled_by='rule:<name>'` so the dashboard can show
        provenance.
        """
        if scratch_mode not in ("volatile", "persistent", "off"):
            raise ValueError(
                f"scratch_mode must be volatile/persistent/off, got {scratch_mode!r}"
            )
        normalised_mac = _normalise_mac(mac)
        # Re-enrolling replaces the row wholesale, which would throw away
        # how this machine is powered — and a machine you can no longer
        # switch on is a machine you've lost. Power wiring is physical
        # and survives a profile change, so carry it across.
        power_columns = (
            "reboot_command", "power_on_command",
            "power_off_command", "pdu_alias",
        )
        with self._write_lock, self._connect() as connection:
            previous = connection.execute(
                f"SELECT {', '.join(power_columns)} FROM machines "
                "WHERE mac = ?",
                (normalised_mac,),
            ).fetchone()
            connection.execute(
                "INSERT OR REPLACE INTO machines "
                "(mac, profile_name, architecture, platform, "
                " serial_console, enrolled_by, scratch_mode) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    normalised_mac, profile_name, architecture,
                    platform, 1 if serial_console else 0, enrolled_by,
                    scratch_mode,
                ),
            )
            if previous is not None:
                assignments = ", ".join(f"{c} = ?" for c in power_columns)
                connection.execute(
                    f"UPDATE machines SET {assignments} WHERE mac = ?",
                    (*(previous[c] for c in power_columns), normalised_mac),
                )
        result = self.lookup(normalised_mac)
        # lookup must succeed -- we just wrote the row inside the lock.
        assert result is not None
        return result

    def update_hostname(self, mac: str, hostname: str) -> None:
        """Stamp the machine's most recently reported hostname.

        Quietly does nothing for MACs not in the registry — boot reporters
        run before the auto-enrol path may have inserted the row, and we
        don't want a transient race to fail status posts.
        """
        normalised_mac = _normalise_mac(mac)
        # Empty/whitespace hostname is unhelpful noise; treat as absent.
        cleaned = (hostname or "").strip()
        if not cleaned:
            return
        with self._write_lock, self._connect() as connection:
            connection.execute(
                "UPDATE machines "
                "SET hostname = ?, hostname_seen_at = datetime('now') "
                "WHERE mac = ?",
                (cleaned, normalised_mac),
            )

    def update_last_ip(self, mac: str, ip: str) -> None:
        """Stamp the IP we observed the machine reporting from."""
        normalised_mac = _normalise_mac(mac)
        cleaned = (ip or "").strip()
        if not cleaned:
            return
        with self._write_lock, self._connect() as connection:
            connection.execute(
                "UPDATE machines "
                "SET last_ip = ?, last_ip_at = datetime('now') "
                "WHERE mac = ?",
                (cleaned, normalised_mac),
            )

    def set_profile(self, mac: str, profile_name: str) -> bool:
        """Move a machine onto a different profile in place.

        Returns True if a row was updated, False if the MAC is unknown —
        callers turn that into a 404 rather than silently succeeding.

        Only the profile changes: architecture, platform, scratch mode,
        serial console and power wiring all stay as they were. That is
        the difference from re-enrolling, which replaces the row.

        The machine keeps running its current image until it next boots;
        arm a reboot (soft or PDU) if you want the change to land now.
        """
        cleaned = (profile_name or "").strip()
        if not cleaned:
            raise ValueError("profile_name must not be empty")
        normalised_mac = _normalise_mac(mac)
        with self._write_lock, self._connect() as connection:
            cursor = connection.execute(
                "UPDATE machines SET profile_name = ? WHERE mac = ?",
                (cleaned, normalised_mac),
            )
            return cursor.rowcount > 0

    def set_pending_reboot(self, mac: str, pending: bool) -> None:
        """Toggle the soft-reboot signal on the machine row.

        When True, the next /status response includes
        `pending_reboot: true` and the in-image reporter calls
        `systemctl reboot`. Used as a fallback after a failed PDU
        power-cycle: even if the PDU is unreachable or unconfigured,
        the machine reboots itself on its next heartbeat.

        Cleared implicitly when the machine re-enrols on its next
        PXE (enroll() resets every field), so the signal naturally
        de-armed once the reboot succeeds.
        """
        normalised_mac = _normalise_mac(mac)
        with self._write_lock, self._connect() as connection:
            connection.execute(
                "UPDATE machines SET pending_reboot = ? WHERE mac = ?",
                (1 if pending else 0, normalised_mac),
            )

    def set_reboot_command(self, mac: str, command: Optional[str]) -> None:
        """Set or clear the machine's reboot shell command. None / empty
        clears it (admin's way of disabling the delete+reboot button)."""
        self.set_power_command(mac, "reboot", command)

    # Which column backs each power action. One dict rather than three
    # near-identical methods, so adding an action later is one line.
    _POWER_COMMAND_COLUMNS = {
        "on": "power_on_command",
        "off": "power_off_command",
        "reboot": "reboot_command",
    }

    def set_power_command(
        self, mac: str, action: str, command: Optional[str],
    ) -> None:
        """Set or clear the shell command for one power action.

        `action` is 'on', 'off' or 'reboot'. None / empty clears the
        command, which falls the machine back to the fleet-wide
        pdudaemon (if configured) for that action.
        """
        column = self._POWER_COMMAND_COLUMNS.get(action)
        if column is None:
            raise ValueError(
                f"unknown power action {action!r}; expected one of "
                + ", ".join(sorted(self._POWER_COMMAND_COLUMNS))
            )
        normalised_mac = _normalise_mac(mac)
        value = (command or "").strip() or None
        with self._write_lock, self._connect() as connection:
            connection.execute(
                f"UPDATE machines SET {column} = ? WHERE mac = ?",
                (value, normalised_mac),
            )

    def set_pdu_alias(self, mac: str, alias: Optional[str]) -> None:
        """Set or clear the machine's pdudaemon alias. Cleared means
        'fall back to the reported hostname'."""
        normalised_mac = _normalise_mac(mac)
        value = (alias or "").strip() or None
        with self._write_lock, self._connect() as connection:
            connection.execute(
                "UPDATE machines SET pdu_alias = ? WHERE mac = ?",
                (value, normalised_mac),
            )

    # ---- Fleet-wide key/value settings ----------------------------------
    #
    # Keys currently recognised:
    #   pdudaemon_host — e.g. "prowl:16421". When set, delete-and-reboot
    #                    on a machine with no explicit reboot_command but
    #                    a known hostname auto-fires
    #                    http://<pdudaemon_host>/power/control/reboot?alias=<hostname>

    def get_setting(self, key: str) -> Optional[str]:
        """Return the stored value for `key`, or None if unset / blank."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM settings WHERE key = ?", (key,),
            ).fetchone()
        if row is None:
            return None
        value = (row["value"] or "").strip()
        return value or None

    def set_setting(self, key: str, value: Optional[str]) -> None:
        """Set or clear a fleet-wide setting. Empty/whitespace clears."""
        cleaned = (value or "").strip()
        with self._write_lock, self._connect() as connection:
            if not cleaned:
                connection.execute(
                    "DELETE FROM settings WHERE key = ?", (key,),
                )
                return
            connection.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET "
                "    value = excluded.value, "
                "    updated_at = datetime('now')",
                (key, cleaned),
            )

    def update_hardware(self, mac: str, hardware_json: str) -> None:
        """Replace the machine row's latest hardware inventory blob.

        Stored as a JSON string; the dashboard parses on render.
        """
        normalised_mac = _normalise_mac(mac)
        cleaned = (hardware_json or "").strip()
        if not cleaned:
            return
        with self._write_lock, self._connect() as connection:
            connection.execute(
                "UPDATE machines "
                "SET last_hardware = ?, "
                "    last_hardware_at = datetime('now') "
                "WHERE mac = ?",
                (cleaned, normalised_mac),
            )

    def update_diagnostics(self, mac: str, diagnostics: str) -> None:
        """Replace the machine row's latest diagnostics blob."""
        normalised_mac = _normalise_mac(mac)
        cleaned = (diagnostics or "").strip()
        if not cleaned:
            return
        with self._write_lock, self._connect() as connection:
            connection.execute(
                "UPDATE machines "
                "SET last_diagnostics = ?, "
                "    last_diagnostics_at = datetime('now') "
                "WHERE mac = ?",
                (cleaned, normalised_mac),
            )

    def update_boot_version(self, mac: str, boot_version: str) -> None:
        """Stamp the image build version the machine is currently running."""
        normalised_mac = _normalise_mac(mac)
        cleaned = (boot_version or "").strip()
        if not cleaned:
            return
        with self._write_lock, self._connect() as connection:
            connection.execute(
                "UPDATE machines "
                "SET boot_version = ?, "
                "    boot_version_seen_at = datetime('now') "
                "WHERE mac = ?",
                (cleaned, normalised_mac),
            )

    _MACHINE_COLUMNS = (
        "mac", "profile_name", "architecture", "platform",
        "serial_console", "enrolled_by", "hostname", "hostname_seen_at",
        "boot_version", "boot_version_seen_at",
        "scratch_mode",
        "last_diagnostics", "last_diagnostics_at",
        "last_hardware", "last_hardware_at",
        "last_ip", "last_ip_at",
        "reboot_command",
        "power_on_command", "power_off_command", "pdu_alias",
        "pending_reboot",
        "created_at",
    )

    def lookup(self, mac: str) -> Optional[Machine]:
        """Return the registered Machine for ``mac``, or None."""
        normalised_mac = _normalise_mac(mac)
        cols = ", ".join(self._MACHINE_COLUMNS)
        with self._connect() as connection:
            row = connection.execute(
                f"SELECT {cols} FROM machines WHERE mac = ?",
                (normalised_mac,),
            ).fetchone()
        if row is None:
            return None
        return _row_to_machine(row)

    def list_all(self) -> list[Machine]:
        cols = ", ".join(self._MACHINE_COLUMNS)
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT {cols} FROM machines ORDER BY created_at, mac"
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
        """Record a state transition. Consecutive same-state events for
        the same MAC are coalesced — the existing row's timestamp is
        bumped to now, no new row inserted. This stops the heartbeat's
        per-2-minute re-report from flooding the events list with
        identical rows.

        State *change* always inserts a new row. detail changes also
        insert a new row (different content is interesting).
        """
        normalised_mac = _normalise_mac(mac)
        with self._write_lock, self._connect() as connection:
            latest = connection.execute(
                "SELECT id, state, detail FROM boot_events "
                "WHERE mac = ? ORDER BY id DESC LIMIT 1",
                (normalised_mac,),
            ).fetchone()
            if (
                latest is not None
                and latest["state"] == state
                and (latest["detail"] or "") == (detail or "")
            ):
                connection.execute(
                    "UPDATE boot_events "
                    "SET occurred_at = datetime('now') WHERE id = ?",
                    (latest["id"],),
                )
            else:
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

    # ---- auto-enrolment rules --------------------------------------------

    def add_auto_enrol_rule(
        self,
        *,
        name: str,
        match_kind: str,
        match_value: str,
        profile_name: str,
        architecture: str = "x86_64",
        # 'any' matches both UEFI and BIOS URLs — the unsurprising
        # default. 'efi'/'pc' gate the rule to one firmware type.
        platform: str = "any",
        serial_console: bool = False,
        scratch_mode: str = "volatile",
    ) -> AutoEnrolRule:
        if scratch_mode not in ("volatile", "persistent", "off"):
            raise ValueError(
                f"scratch_mode must be volatile/persistent/off, got {scratch_mode!r}"
            )
        normalised = _normalise_match_value(match_kind, match_value)
        with self._write_lock, self._connect() as connection:
            cursor = connection.execute(
                "INSERT INTO auto_enrol_rules "
                "(name, match_kind, match_value, profile_name, "
                " architecture, platform, serial_console, scratch_mode) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    name, match_kind, normalised, profile_name,
                    architecture, platform, 1 if serial_console else 0,
                    scratch_mode,
                ),
            )
            rule_id = cursor.lastrowid
        result = self.get_auto_enrol_rule(rule_id)
        assert result is not None
        return result

    def get_auto_enrol_rule(self, rule_id: int) -> Optional[AutoEnrolRule]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT id, name, match_kind, match_value, profile_name, "
                "       architecture, platform, serial_console, "
                "       scratch_mode, created_at "
                "FROM auto_enrol_rules WHERE id = ?",
                (rule_id,),
            ).fetchone()
        return _row_to_rule(row) if row else None

    def list_auto_enrol_rules(self) -> list[AutoEnrolRule]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT id, name, match_kind, match_value, profile_name, "
                "       architecture, platform, serial_console, "
                "       scratch_mode, created_at "
                "FROM auto_enrol_rules ORDER BY id"
            ).fetchall()
        return [_row_to_rule(r) for r in rows]

    def update_auto_enrol_rule(
        self,
        rule_id: int,
        *,
        name: str,
        match_kind: str,
        match_value: str,
        profile_name: str,
        architecture: str = "x86_64",
        platform: str = "any",
        serial_console: bool = False,
        scratch_mode: str = "volatile",
    ) -> Optional[AutoEnrolRule]:
        """Replace every editable field on a rule in-place. Returns
        the refreshed row, or None if the rule_id didn't exist.

        Caller is responsible for value validation (match_kind /
        platform / scratch_mode) — same surface as add_auto_enrol_rule.
        """
        if scratch_mode not in ("volatile", "persistent", "off"):
            raise ValueError(
                f"scratch_mode must be volatile/persistent/off, got {scratch_mode!r}"
            )
        normalised = _normalise_match_value(match_kind, match_value)
        with self._write_lock, self._connect() as connection:
            # A machine's `enrolled_by` is a snapshot ('rule:<name>')
            # taken when the rule enrolled it, not a live reference. So a
            # rename has to carry across to every machine this rule
            # enrolled, or the machines list keeps showing the old name.
            # Read the current name first so we know which snapshot to
            # rewrite.
            previous_name_row = connection.execute(
                "SELECT name FROM auto_enrol_rules WHERE id = ?",
                (rule_id,),
            ).fetchone()
            cursor = connection.execute(
                "UPDATE auto_enrol_rules SET "
                "    name = ?, match_kind = ?, match_value = ?, "
                "    profile_name = ?, architecture = ?, platform = ?, "
                "    serial_console = ?, scratch_mode = ? "
                "WHERE id = ?",
                (
                    name, match_kind, normalised, profile_name,
                    architecture, platform, 1 if serial_console else 0,
                    scratch_mode, rule_id,
                ),
            )
            if cursor.rowcount == 0:
                return None
            previous_name = previous_name_row["name"]
            if previous_name != name:
                connection.execute(
                    "UPDATE machines SET enrolled_by = ? "
                    "WHERE enrolled_by = ?",
                    (f"rule:{name}", f"rule:{previous_name}"),
                )
        return self.get_auto_enrol_rule(rule_id)

    def remove_auto_enrol_rule(self, rule_id: int) -> bool:
        with self._write_lock, self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM auto_enrol_rules WHERE id = ?", (rule_id,)
            )
            return cursor.rowcount > 0

    def find_matching_rule(
        self,
        mac: str,
        source_ip: Optional[str] = None,
        platform: Optional[str] = None,
    ) -> Optional[AutoEnrolRule]:
        """Return the first rule (lowest id) whose predicate matches.

        Empty match_value means "match anything of this kind" — useful for
        a catch-all `mac_prefix=""` rule that auto-enrols every unknown MAC
        to a single registration profile.

        A rule whose ``platform`` is one of ``'efi'``/``'pc'`` only matches
        when the URL's platform is the same; ``platform='any'`` matches
        irrespective. This lets admins ship per-firmware rules (e.g. a
        UEFI rule and a BIOS rule on the same subnet with different
        serial-console defaults) without writing custom matchers.
        """
        normalised_mac = _normalise_mac(mac)
        for rule in self.list_auto_enrol_rules():
            if _rule_matches(
                rule,
                mac=normalised_mac,
                source_ip=source_ip,
                platform=platform,
            ):
                return rule
        return None

    def remove(self, mac: str) -> bool:
        """Delete a machine and its associated boot events + sessions.

        We don't enforce FK CASCADE in the schema (it adds migration
        complexity for a deployment that hasn't needed cleanup yet);
        instead we explicitly delete the dependents in the same write
        lock so the cleanup is atomic.
        """
        normalised_mac = _normalise_mac(mac)
        with self._write_lock, self._connect() as connection:
            connection.execute(
                "DELETE FROM boot_events WHERE mac = ?", (normalised_mac,)
            )
            # boot_sessions only exists when a persistent BootSessionStore
            # has run schema setup on the same DB file — registry-only
            # tests skip it. Tolerate missing table.
            try:
                connection.execute(
                    "DELETE FROM boot_sessions WHERE mac = ?",
                    (normalised_mac,),
                )
            except sqlite3.OperationalError:
                pass
            cursor = connection.execute(
                "DELETE FROM machines WHERE mac = ?", (normalised_mac,)
            )
            return cursor.rowcount > 0


# ---- Helpers --------------------------------------------------------------


def _normalise_mac(mac: str) -> str:
    """Same normalisation rule as BootSessionStore — lowercase colon form."""
    return mac.replace("-", ":").replace(".", ":").lower().strip()


def _normalise_match_value(match_kind: str, match_value: str) -> str:
    """Canonicalise the value so the same rule isn't entered twice."""
    if match_kind == "mac_prefix":
        cleaned = match_value.replace("-", ":").replace(".", ":").lower().strip()
        # Trailing colon is optional in input; we strip both ways and store
        # the bare hex+colon prefix so matching is a simple startswith.
        return cleaned.rstrip(":")
    if match_kind == "ip_cidr":
        import ipaddress

        # ipaddress.ip_network normalises notation (e.g. host bits stripped).
        return str(ipaddress.ip_network(match_value.strip(), strict=False))
    raise ValueError(f"unknown match_kind: {match_kind!r}")


def _rule_matches(
    rule: "AutoEnrolRule",
    *,
    mac: str,
    source_ip: Optional[str],
    platform: Optional[str] = None,
) -> bool:
    """Return True if the rule matches this (mac, source_ip, platform) tuple."""
    # Platform gate: a rule with platform 'any' matches regardless. A
    # rule with platform 'efi' / 'pc' only matches a URL of that
    # platform; if the URL didn't include a platform, we conservatively
    # decline so a UEFI-only rule can't accidentally fire for a BIOS box
    # that happened to omit the field.
    rule_platform = (rule.platform or "any").lower()
    if rule_platform != "any":
        if platform is None or platform.lower() != rule_platform:
            return False
    if rule.match_kind == "mac_prefix":
        if rule.match_value == "":
            return True
        return mac.startswith(rule.match_value)
    if rule.match_kind == "ip_cidr":
        if source_ip is None:
            return False
        import ipaddress

        try:
            return (
                ipaddress.ip_address(source_ip)
                in ipaddress.ip_network(rule.match_value, strict=False)
            )
        except ValueError:
            return False
    return False


def _row_to_rule(row: sqlite3.Row) -> "AutoEnrolRule":
    return AutoEnrolRule(
        id=row["id"],
        name=row["name"],
        match_kind=row["match_kind"],
        match_value=row["match_value"],
        profile_name=row["profile_name"],
        architecture=row["architecture"],
        platform=row["platform"],
        serial_console=bool(row["serial_console"]),
        scratch_mode=row["scratch_mode"] or "volatile",
        created_at=row["created_at"],
    )


# amd64/x86_64/x64 all name the same wire-format arch — Debian uses
# "amd64" for build artefacts, but the registry stores whatever the PXE
# boot loader announced ("x86_64" for BIOS, "x64" for EFI). Same story
# for arm64/aarch64. Anything selecting machines by architecture has to
# treat these as one, so the mapping lives here rather than in each
# caller.
ARCHITECTURE_ALIASES: dict[str, tuple[str, ...]] = {
    "amd64": ("amd64", "x86_64", "x64"),
    "x86_64": ("amd64", "x86_64", "x64"),
    "x64": ("amd64", "x86_64", "x64"),
    "arm64": ("arm64", "aarch64"),
    "aarch64": ("arm64", "aarch64"),
}


def architecture_aliases(architecture: str) -> tuple[str, ...]:
    """Every spelling of `architecture`, including itself."""
    return ARCHITECTURE_ALIASES.get(architecture, (architecture,))


def matches_architecture(candidate: str, wanted: str) -> bool:
    """True when two architecture strings name the same architecture."""
    return candidate in architecture_aliases(wanted)


def _optional_column(row: sqlite3.Row, name: str):
    """Read a column that older databases may predate. Migrations add
    it on open, so this only matters for rows read from a connection
    opened before the migration ran."""
    return row[name] if name in row.keys() else None


def _row_to_machine(row: sqlite3.Row) -> Machine:
    return Machine(
        mac=row["mac"],
        profile_name=row["profile_name"],
        architecture=row["architecture"],
        platform=row["platform"],
        serial_console=bool(row["serial_console"]),
        enrolled_by=row["enrolled_by"] if row["enrolled_by"] else "manual",
        hostname=row["hostname"],
        hostname_seen_at=row["hostname_seen_at"],
        boot_version=row["boot_version"],
        boot_version_seen_at=row["boot_version_seen_at"],
        scratch_mode=row["scratch_mode"] or "volatile",
        last_diagnostics=row["last_diagnostics"],
        last_diagnostics_at=row["last_diagnostics_at"],
        last_hardware=row["last_hardware"],
        last_hardware_at=row["last_hardware_at"],
        last_ip=row["last_ip"],
        last_ip_at=row["last_ip_at"],
        reboot_command=row["reboot_command"],
        power_on_command=_optional_column(row, "power_on_command"),
        power_off_command=_optional_column(row, "power_off_command"),
        pdu_alias=_optional_column(row, "pdu_alias"),
        pending_reboot=bool(row["pending_reboot"]) if (
            "pending_reboot" in row.keys()
        ) else False,
        created_at=row["created_at"],
    )
