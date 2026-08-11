"""Registry-level tests for the reservation system.

Reservations are the one ownership record that serves both permanent
fleet machines and transient compute leases. A machine is AVAILABLE
(claimable) exactly when it has no active reservation.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fleetboot.server.registry import (
    AVAILABILITY_AVAILABLE,
    AVAILABILITY_PERMANENT,
    AVAILABILITY_RESERVED,
    MachineRegistry,
)


def _registry(tmp_path: Path) -> MachineRegistry:
    return MachineRegistry(tmp_path / "machines.sqlite")


def _enrol(reg: MachineRegistry, mac: str, arch: str = "x86_64") -> None:
    reg.enroll(
        mac=mac, profile_name="default", architecture=arch, platform="efi",
    )


def _force_expire(reg: MachineRegistry, mac: str) -> None:
    """Backdate an active lease's expiry so the reaper/derivation see it
    as expired (tests can't wait out a real TTL)."""
    with reg._connect() as connection:  # noqa: SLF001 — test reaches in
        connection.execute(
            "UPDATE reservations SET expires_at = datetime('now', '-1 hour') "
            "WHERE mac = ? AND released_at IS NULL",
            (mac,),
        )


# ---- Default posture: enrol pins permanent -----------------------------


def test_new_machine_is_permanent_and_not_claimable(tmp_path: Path):
    reg = _registry(tmp_path)
    _enrol(reg, "aa:bb:cc:00:00:01")
    assert reg.availability("aa:bb:cc:00:00:01") == AVAILABILITY_PERMANENT
    res = reg.active_reservation("aa:bb:cc:00:00:01")
    assert res is not None and res.lease_kind == "permanent"
    # A reserve request finds nothing: everything is protected by default.
    assert reg.reserve(
        profile="claude-compute", count=4, holder="agent:x", ttl_seconds=3600,
    ) == []


def test_release_to_pool_makes_it_available(tmp_path: Path):
    reg = _registry(tmp_path)
    _enrol(reg, "aa:bb:cc:00:00:01")
    assert reg.release_to_pool("aa:bb:cc:00:00:01") is True
    assert reg.availability("aa:bb:cc:00:00:01") == AVAILABILITY_AVAILABLE
    # release_to_pool on an already-free machine is a no-op.
    assert reg.release_to_pool("aa:bb:cc:00:00:01") is False


# ---- Leasing -----------------------------------------------------------


def _pool_machine(reg: MachineRegistry, mac: str, arch: str = "x86_64") -> None:
    _enrol(reg, mac, arch)
    reg.release_to_pool(mac)


def test_reserve_leases_available_machines_and_pins_profile(tmp_path: Path):
    reg = _registry(tmp_path)
    for i in range(3):
        _pool_machine(reg, f"aa:bb:cc:00:00:0{i}")
    leased = reg.reserve(
        profile="claude-compute", count=2, holder="agent:kbuild",
        ttl_seconds=3600, purpose="kernel build",
    )
    assert len(leased) == 2
    for m in leased:
        assert m.profile_name == "claude-compute"        # profile pinned
        assert reg.availability(m.mac) == AVAILABILITY_RESERVED
        res = reg.active_reservation(m.mac)
        assert res.holder == "agent:kbuild"
        assert res.lease_kind == "lease"
        assert res.expires_at is not None
    # Deterministic pick: the two lowest MACs.
    assert sorted(m.mac for m in leased) == [
        "aa:bb:cc:00:00:00", "aa:bb:cc:00:00:01",
    ]


def test_reserve_is_short_when_pool_is_small(tmp_path: Path):
    reg = _registry(tmp_path)
    _pool_machine(reg, "aa:bb:cc:00:00:01")
    leased = reg.reserve(
        profile="claude-compute", count=5, holder="agent:x", ttl_seconds=3600,
    )
    assert len(leased) == 1  # only one was available


def test_reserve_does_not_double_claim(tmp_path: Path):
    reg = _registry(tmp_path)
    for i in range(2):
        _pool_machine(reg, f"aa:bb:cc:00:00:0{i}")
    first = reg.reserve(
        profile="p", count=2, holder="agent:a", ttl_seconds=3600,
    )
    assert len(first) == 2
    # Everything is now leased; a second agent gets nothing.
    second = reg.reserve(
        profile="p", count=2, holder="agent:b", ttl_seconds=3600,
    )
    assert second == []


def test_reserve_filters_by_architecture(tmp_path: Path):
    reg = _registry(tmp_path)
    _pool_machine(reg, "aa:bb:cc:00:00:01", arch="x86_64")
    _pool_machine(reg, "aa:bb:cc:00:00:02", arch="arm64")
    leased = reg.reserve(
        profile="p", count=5, holder="agent:x", ttl_seconds=3600,
        architecture="arm64",
    )
    assert [m.mac for m in leased] == ["aa:bb:cc:00:00:02"]


# ---- Renew / release / reap --------------------------------------------


def test_renew_extends_a_lease(tmp_path: Path):
    reg = _registry(tmp_path)
    _pool_machine(reg, "aa:bb:cc:00:00:01")
    reg.reserve(profile="p", count=1, holder="agent:x", ttl_seconds=60)
    before = reg.active_reservation("aa:bb:cc:00:00:01").expires_at
    assert reg.renew("aa:bb:cc:00:00:01", ttl_seconds=7200) is True
    after = reg.active_reservation("aa:bb:cc:00:00:01").expires_at
    assert after > before
    # A permanent hold can't be renewed.
    _enrol(reg, "aa:bb:cc:00:00:02")
    assert reg.renew("aa:bb:cc:00:00:02", ttl_seconds=60) is False


def test_release_frees_a_lease_but_not_a_permanent_hold(tmp_path: Path):
    reg = _registry(tmp_path)
    _pool_machine(reg, "aa:bb:cc:00:00:01")
    reg.reserve(profile="p", count=1, holder="agent:x", ttl_seconds=3600)
    assert reg.release(macs=["aa:bb:cc:00:00:01"]) == ["aa:bb:cc:00:00:01"]
    assert reg.availability("aa:bb:cc:00:00:01") == AVAILABILITY_AVAILABLE
    # release must not free a permanent (school) machine.
    _enrol(reg, "aa:bb:cc:00:00:09")
    assert reg.release(macs=["aa:bb:cc:00:00:09"]) == []
    assert reg.availability("aa:bb:cc:00:00:09") == AVAILABILITY_PERMANENT


def test_release_by_holder(tmp_path: Path):
    reg = _registry(tmp_path)
    for i in range(3):
        _pool_machine(reg, f"aa:bb:cc:00:00:0{i}")
    reg.reserve(profile="p", count=2, holder="agent:a", ttl_seconds=3600)
    reg.reserve(profile="p", count=1, holder="agent:b", ttl_seconds=3600)
    freed = reg.release(holder="agent:a")
    assert len(freed) == 2
    # agent:b's lease is untouched.
    assert reg.availability("aa:bb:cc:00:00:02") == AVAILABILITY_RESERVED


def test_reap_expired_frees_only_expired_leases(tmp_path: Path):
    reg = _registry(tmp_path)
    _pool_machine(reg, "aa:bb:cc:00:00:01")
    _pool_machine(reg, "aa:bb:cc:00:00:02")
    reg.reserve(profile="p", count=1, holder="agent:x", ttl_seconds=3600)
    # Second machine leased then backdated to expired.
    reg.reserve(profile="p", count=1, holder="agent:y", ttl_seconds=3600)
    # Whichever the second machine is, force it expired.
    _force_expire(reg, "aa:bb:cc:00:00:02")
    freed = reg.reap_expired()
    assert freed == ["aa:bb:cc:00:00:02"]
    assert reg.availability("aa:bb:cc:00:00:02") == AVAILABILITY_AVAILABLE
    # The live lease is untouched.
    assert reg.availability("aa:bb:cc:00:00:01") == AVAILABILITY_RESERVED


def test_expired_lease_reads_as_available_even_before_reaping(tmp_path: Path):
    reg = _registry(tmp_path)
    _pool_machine(reg, "aa:bb:cc:00:00:01")
    reg.reserve(profile="p", count=1, holder="agent:x", ttl_seconds=3600)
    _force_expire(reg, "aa:bb:cc:00:00:01")
    # Availability derivation treats an expired lease as free immediately.
    assert reg.availability("aa:bb:cc:00:00:01") == AVAILABILITY_AVAILABLE
    # And a fresh reserve can claim it despite the unreaped row.
    leased = reg.reserve(
        profile="p2", count=1, holder="agent:z", ttl_seconds=3600,
    )
    assert [m.mac for m in leased] == ["aa:bb:cc:00:00:01"]


# ---- Pin / admin -------------------------------------------------------


def test_pin_takes_a_leased_machine_back_out_of_the_pool(tmp_path: Path):
    reg = _registry(tmp_path)
    _pool_machine(reg, "aa:bb:cc:00:00:01")
    reg.reserve(profile="p", count=1, holder="agent:x", ttl_seconds=3600)
    res = reg.pin(
        "aa:bb:cc:00:00:01", holder="school", profile="cinnamon-desktop",
    )
    assert res is not None and res.lease_kind == "permanent"
    assert reg.availability("aa:bb:cc:00:00:01") == AVAILABILITY_PERMANENT
    # Pinning with a profile switches the machine to it.
    assert reg.lookup("aa:bb:cc:00:00:01").profile_name == "cinnamon-desktop"


def test_pin_unknown_machine_returns_none(tmp_path: Path):
    reg = _registry(tmp_path)
    assert reg.pin("aa:bb:cc:00:00:99", holder="school") is None


def test_delete_removes_reservations(tmp_path: Path):
    reg = _registry(tmp_path)
    _enrol(reg, "aa:bb:cc:00:00:01")
    assert reg.active_reservation("aa:bb:cc:00:00:01") is not None
    reg.remove("aa:bb:cc:00:00:01")
    assert reg.active_reservation("aa:bb:cc:00:00:01") is None


# ---- Migration: pin pre-existing machines once -------------------------


def test_migration_pins_pre_reservation_machines(tmp_path: Path):
    """A machine that existed before reservations shipped must come up
    permanent, so it can't be grabbed for compute by surprise."""
    db = tmp_path / "machines.sqlite"
    reg = _registry(tmp_path)
    _enrol(reg, "aa:bb:cc:00:00:01")
    # Simulate a pre-reservation DB: drop the machine's reservation and
    # the ran-once flag, leaving a bare machines row.
    with reg._connect() as connection:  # noqa: SLF001
        connection.execute("DELETE FROM reservations")
        connection.execute(
            "DELETE FROM settings WHERE key = 'reservations_pinned_existing'"
        )
    assert reg.availability("aa:bb:cc:00:00:01") == AVAILABILITY_AVAILABLE
    # Re-opening runs the one-time migration.
    reg2 = MachineRegistry(db)
    assert reg2.availability("aa:bb:cc:00:00:01") == AVAILABILITY_PERMANENT


def test_migration_runs_only_once(tmp_path: Path):
    """A machine an admin released to the pool must not be re-pinned when
    the server restarts."""
    db = tmp_path / "machines.sqlite"
    reg = _registry(tmp_path)
    _enrol(reg, "aa:bb:cc:00:00:01")
    reg.release_to_pool("aa:bb:cc:00:00:01")
    assert reg.availability("aa:bb:cc:00:00:01") == AVAILABILITY_AVAILABLE
    reg2 = MachineRegistry(db)  # restart
    assert reg2.availability("aa:bb:cc:00:00:01") == AVAILABILITY_AVAILABLE
