"""Tests for the in-process TFTP-routed grub-event handler.

This is the callback fleetboot registers as tftpjail's `rrq_intercept`.
It recognises `/grub-event/<token>/<state>` RRQ paths, records the
boot event against the BootSessionStore + MachineRegistry, and returns
empty bytes back to tftpjail so GRUB's `source` parses a no-op script.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fleetboot.boot_states import BootState
from fleetboot.server.boot_sessions import BootSessionStore
from fleetboot.server.grub_event_intercept import make_grub_event_intercept
from fleetboot.server.registry import MachineRegistry


# Loopback address used in tests — the callback ignores it but must be
# given a real shape (host, port).
_CLIENT_ADDR = ("192.0.2.10", 12345)


@pytest.fixture
def sessions() -> BootSessionStore:
    return BootSessionStore()


@pytest.fixture
def registry(tmp_path: Path) -> MachineRegistry:
    reg = MachineRegistry(tmp_path / "machines.sqlite")
    reg.enroll(
        mac="aa:bb:cc:dd:ee:ff", profile_name="default",
        architecture="x86_64", platform="efi",
    )
    return reg


def test_intercept_records_state_and_returns_empty(
    sessions: BootSessionStore, registry: MachineRegistry,
):
    """Happy path: known token, known state. The boot session and
    persistent boot_events table both get updated; return value is
    empty bytes (NOT None) so tftpjail sends a zero-length file back
    to GRUB."""
    session = sessions.mint("aa:bb:cc:dd:ee:ff")
    intercept = make_grub_event_intercept(
        sessions=sessions, registry=registry,
    )

    result = intercept(
        f"/grub-event/{session.token}/grub_running", _CLIENT_ADDR,
    )

    assert result == b""
    refreshed = sessions.lookup(session.token)
    assert refreshed is not None
    assert refreshed.latest_state == BootState.GRUB_RUNNING
    events = registry.recent_boot_events(mac="aa:bb:cc:dd:ee:ff")
    assert any(e.state == "grub_running" for e in events)


def test_intercept_returns_none_for_non_grub_event_paths(
    sessions: BootSessionStore,
):
    """Anything that doesn't match the /grub-event/<token>/<state>
    shape returns None so tftpjail falls through to its normal
    dispatch (public-assets, jail policy)."""
    intercept = make_grub_event_intercept(sessions=sessions)

    assert intercept("vmlinuz", _CLIENT_ADDR) is None
    assert intercept("/jail/aa:bb:cc:dd:ee:ff/x86_64/efi", _CLIENT_ADDR) is None
    # Wrong number of segments.
    assert intercept("/grub-event", _CLIENT_ADDR) is None
    assert intercept("/grub-event/just-one", _CLIENT_ADDR) is None
    assert intercept(
        "/grub-event/too/many/segments/here", _CLIENT_ADDR,
    ) is None
    # First segment must be exactly "grub-event" (not "grub_event" etc.).
    assert intercept("/something-else/token/state", _CLIENT_ADDR) is None


def test_intercept_unknown_state_returns_empty_without_recording(
    sessions: BootSessionStore, registry: MachineRegistry,
):
    """An unknown state means renderer + server are out of sync — we'd
    rather no-op silently than break the boot. tftpjail still gets an
    empty body to send back so GRUB doesn't error."""
    session = sessions.mint("aa:bb:cc:dd:ee:ff")
    intercept = make_grub_event_intercept(
        sessions=sessions, registry=registry,
    )

    result = intercept(
        f"/grub-event/{session.token}/not-a-real-state", _CLIENT_ADDR,
    )

    assert result == b""
    # State not recorded.
    refreshed = sessions.lookup(session.token)
    assert refreshed is not None
    assert refreshed.latest_state is None
    assert registry.recent_boot_events(mac="aa:bb:cc:dd:ee:ff") == []


def test_intercept_unknown_token_returns_empty_without_recording(
    sessions: BootSessionStore, registry: MachineRegistry,
):
    """A stale or fake token — same reasoning as unknown state. Empty
    body, no recording."""
    intercept = make_grub_event_intercept(
        sessions=sessions, registry=registry,
    )

    result = intercept(
        "/grub-event/totally-fake-token/grub_running", _CLIENT_ADDR,
    )

    assert result == b""
    assert registry.recent_boot_events(mac="aa:bb:cc:dd:ee:ff") == []


def test_intercept_out_of_order_state_returns_empty_without_recording(
    sessions: BootSessionStore, registry: MachineRegistry,
):
    """A later state followed by an earlier one (e.g. heartbeat race)
    — the BootSessionStore rejects it; we don't propagate the
    rejection to GRUB."""
    session = sessions.mint("aa:bb:cc:dd:ee:ff")
    sessions.record_state(session.token, BootState.LOGIN_CONSOLE)
    intercept = make_grub_event_intercept(
        sessions=sessions, registry=registry,
    )

    result = intercept(
        f"/grub-event/{session.token}/grub_running", _CLIENT_ADDR,
    )

    assert result == b""
    # latest_state still LOGIN_CONSOLE (the earlier-state attempt was
    # silently dropped).
    refreshed = sessions.lookup(session.token)
    assert refreshed is not None
    assert refreshed.latest_state == BootState.LOGIN_CONSOLE


def test_intercept_without_registry_still_records_session_state(
    sessions: BootSessionStore,
):
    """Registry is optional — the session-store update still happens
    so the dashboard's in-memory state reflects the GRUB lifecycle
    even when no persistent boot_events row is being written."""
    session = sessions.mint("aa:bb:cc:dd:ee:ff")
    intercept = make_grub_event_intercept(sessions=sessions)

    result = intercept(
        f"/grub-event/{session.token}/grub_running", _CLIENT_ADDR,
    )

    assert result == b""
    refreshed = sessions.lookup(session.token)
    assert refreshed is not None
    assert refreshed.latest_state == BootState.GRUB_RUNNING
