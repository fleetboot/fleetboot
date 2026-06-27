"""Tests for the boot-session token store."""

import pytest

from openschool.boot_states import BootState
from openschool.server.boot_sessions import (
    BootSessionStore,
    OutOfOrderStateError,
    UnknownTokenError,
)


def test_mint_creates_unique_high_entropy_tokens():
    store = BootSessionStore()
    first = store.mint("aa:bb:cc:dd:ee:ff")
    second = store.mint("aa:bb:cc:dd:ee:ff")
    assert first.token != second.token
    # 32 bytes -> 64 hex chars. Anything noticeably shorter means we lost
    # entropy somewhere.
    assert len(first.token) >= 64


def test_mint_normalises_mac_format():
    store = BootSessionStore()
    via_dashes = store.mint("AA-BB-CC-DD-EE-FF")
    via_colons = store.mint("aa:bb:cc:dd:ee:ff")
    assert via_dashes.mac == "aa:bb:cc:dd:ee:ff"
    assert via_colons.mac == "aa:bb:cc:dd:ee:ff"


def test_record_state_in_order_succeeds():
    store = BootSessionStore()
    session = store.mint("aa:bb:cc:dd:ee:ff")
    for state in (
        BootState.NETWORK_UP,
        BootState.NFS_MOUNTED,
        BootState.LOGIN_READY,
        BootState.USER_LOGGED_IN,
    ):
        store.record_state(session.token, state)
    refreshed = store.lookup(session.token)
    assert refreshed is not None
    assert refreshed.latest_state == BootState.USER_LOGGED_IN
    assert refreshed.reports == [
        BootState.NETWORK_UP,
        BootState.NFS_MOUNTED,
        BootState.LOGIN_READY,
        BootState.USER_LOGGED_IN,
    ]


def test_record_state_repeats_are_idempotent():
    store = BootSessionStore()
    session = store.mint("aa:bb:cc:dd:ee:ff")
    store.record_state(session.token, BootState.NETWORK_UP)
    store.record_state(session.token, BootState.NETWORK_UP)
    refreshed = store.lookup(session.token)
    assert refreshed is not None
    assert refreshed.latest_state == BootState.NETWORK_UP


def test_record_state_rejects_out_of_order():
    store = BootSessionStore()
    session = store.mint("aa:bb:cc:dd:ee:ff")
    store.record_state(session.token, BootState.LOGIN_READY)
    with pytest.raises(OutOfOrderStateError):
        store.record_state(session.token, BootState.NETWORK_UP)


def test_record_state_unknown_token_raises():
    store = BootSessionStore()
    with pytest.raises(UnknownTokenError):
        store.record_state("not-a-real-token", BootState.NETWORK_UP)


def test_end_session_removes_it():
    store = BootSessionStore()
    session = store.mint("aa:bb:cc:dd:ee:ff")
    store.end(session.token)
    assert store.lookup(session.token) is None
    with pytest.raises(UnknownTokenError):
        store.record_state(session.token, BootState.NETWORK_UP)


def test_active_sessions_snapshot():
    store = BootSessionStore()
    one = store.mint("aa:bb:cc:dd:ee:01")
    two = store.mint("aa:bb:cc:dd:ee:02")
    active = {s.token for s in store.active_sessions()}
    assert active == {one.token, two.token}
