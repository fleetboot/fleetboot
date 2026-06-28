"""Tests for the SQLite-backed BootSessionStore.

The in-memory mode is already covered by tests/test_status_endpoint.py and
tests/test_boot_sessions.py (if present). This file targets the
persistence properties: tokens survive across BootSessionStore instances,
which is what makes server restarts non-destructive.
"""

from pathlib import Path

import pytest

from fleetboot.boot_states import BootState
from fleetboot.server.boot_sessions import (
    BootSessionStore,
    OutOfOrderStateError,
    UnknownTokenError,
)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """A fresh sqlite path per test."""
    return tmp_path / "sessions.sqlite"


def test_mint_and_lookup_round_trip(db_path: Path):
    store = BootSessionStore(db_path)
    session = store.mint("aa:bb:cc:dd:ee:ff")
    found = store.lookup(session.token)
    assert found is not None
    assert found.token == session.token
    assert found.mac == "aa:bb:cc:dd:ee:ff"
    assert found.latest_state is None


def test_session_survives_a_new_store_instance(db_path: Path):
    """Server restart → fresh BootSessionStore over the same DB file →
    pre-existing tokens still resolve."""
    first = BootSessionStore(db_path)
    session = first.mint("aa:bb:cc:dd:ee:ff")
    first.record_state(session.token, BootState.NETWORK_UP)

    # Simulate restart: drop the first store, build a new one over the
    # same file. The token must still be valid.
    second = BootSessionStore(db_path)
    refreshed = second.lookup(session.token)
    assert refreshed is not None
    assert refreshed.mac == "aa:bb:cc:dd:ee:ff"
    assert refreshed.latest_state == BootState.NETWORK_UP


def test_record_state_persists_progression(db_path: Path):
    store = BootSessionStore(db_path)
    session = store.mint("aa:bb:cc:dd:ee:ff")
    for state in (BootState.NETWORK_UP, BootState.NFS_MOUNTED, BootState.LOGIN_READY):
        store.record_state(session.token, state)
    later = BootSessionStore(db_path).lookup(session.token)
    assert later is not None
    assert later.latest_state == BootState.LOGIN_READY


def test_same_state_heartbeat_does_not_regress(db_path: Path):
    """Heartbeats re-send the current state; the store must accept that
    without raising and without dropping back."""
    store = BootSessionStore(db_path)
    session = store.mint("aa:bb:cc:dd:ee:ff")
    store.record_state(session.token, BootState.USER_LOGGED_IN)
    # Two heartbeat reports — must not raise, must keep latest_state pinned.
    store.record_state(session.token, BootState.USER_LOGGED_IN)
    store.record_state(session.token, BootState.USER_LOGGED_IN)
    refreshed = store.lookup(session.token)
    assert refreshed is not None
    assert refreshed.latest_state == BootState.USER_LOGGED_IN


def test_out_of_order_report_still_rejected(db_path: Path):
    store = BootSessionStore(db_path)
    session = store.mint("aa:bb:cc:dd:ee:ff")
    store.record_state(session.token, BootState.LOGIN_READY)
    with pytest.raises(OutOfOrderStateError):
        store.record_state(session.token, BootState.NETWORK_UP)


def test_unknown_token_still_raises(db_path: Path):
    store = BootSessionStore(db_path)
    with pytest.raises(UnknownTokenError):
        store.record_state("not-a-real-token", BootState.NETWORK_UP)


def test_end_removes_session(db_path: Path):
    store = BootSessionStore(db_path)
    session = store.mint("aa:bb:cc:dd:ee:ff")
    store.end(session.token)
    assert store.lookup(session.token) is None


def test_last_seen_by_mac_returns_latest_timestamp_per_mac(db_path: Path):
    store = BootSessionStore(db_path)
    a = store.mint("aa:bb:cc:dd:ee:01")
    b = store.mint("aa:bb:cc:dd:ee:02")
    store.record_state(a.token, BootState.NETWORK_UP)
    store.record_state(b.token, BootState.LOGIN_READY)
    seen = store.last_seen_by_mac()
    assert sorted(seen.keys()) == ["aa:bb:cc:dd:ee:01", "aa:bb:cc:dd:ee:02"]
    # Both values must be parseable as SQLite-style ISO timestamps.
    from datetime import datetime
    for mac, ts in seen.items():
        # Should not raise.
        datetime.fromisoformat(ts)


def test_last_seen_skips_never_reported_sessions(db_path: Path):
    """Sessions that have only been minted (no state report) shouldn't
    populate last_seen — there's nothing to relativise."""
    store = BootSessionStore(db_path)
    store.mint("aa:bb:cc:dd:ee:01")  # never reports
    assert store.last_seen_by_mac() == {}


def test_active_sessions_lists_persisted_rows(db_path: Path):
    store = BootSessionStore(db_path)
    store.mint("aa:bb:cc:dd:ee:01")
    store.mint("aa:bb:cc:dd:ee:02")
    # Fresh store instance over same file — both rows must show.
    refreshed = BootSessionStore(db_path).active_sessions()
    macs = sorted(s.mac for s in refreshed)
    assert macs == ["aa:bb:cc:dd:ee:01", "aa:bb:cc:dd:ee:02"]
