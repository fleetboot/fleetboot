"""Tests for the background lease reaper."""

from __future__ import annotations

from pathlib import Path

from fleetboot.server.registry import (
    AVAILABILITY_AVAILABLE,
    AVAILABILITY_RESERVED,
    MachineRegistry,
)
from fleetboot.server.reservation_reaper import ReservationReaper


def _registry(tmp_path: Path) -> MachineRegistry:
    reg = MachineRegistry(tmp_path / "machines.sqlite")
    return reg


def _pool_machine(reg: MachineRegistry, mac: str) -> None:
    reg.enroll(
        mac=mac, profile_name="default", architecture="x86_64", platform="efi",
    )
    reg.release_to_pool(mac)


def _force_expire(reg: MachineRegistry, mac: str) -> None:
    with reg._connect() as connection:  # noqa: SLF001
        connection.execute(
            "UPDATE reservations SET expires_at = datetime('now', '-1 hour') "
            "WHERE mac = ? AND released_at IS NULL",
            (mac,),
        )


def test_reaper_releases_expired_leases_and_powers_them_off(tmp_path: Path):
    reg = _registry(tmp_path)
    _pool_machine(reg, "aa:bb:cc:00:00:01")
    _pool_machine(reg, "aa:bb:cc:00:00:02")
    reg.reserve(profile="claude-compute", count=2, holder="agent:x", ttl_seconds=3600)
    _force_expire(reg, "aa:bb:cc:00:00:01")  # only the first expires

    powered_off: list[str] = []
    reaper = ReservationReaper(
        registry=reg, power_off=powered_off.append,
    )
    freed = reaper.reap_once()

    assert freed == ["aa:bb:cc:00:00:01"]
    assert powered_off == ["aa:bb:cc:00:00:01"]
    assert reg.availability("aa:bb:cc:00:00:01") == AVAILABILITY_AVAILABLE
    # The live lease is untouched.
    assert reg.availability("aa:bb:cc:00:00:02") == AVAILABILITY_RESERVED


def test_reaper_noop_when_nothing_expired(tmp_path: Path):
    reg = _registry(tmp_path)
    _pool_machine(reg, "aa:bb:cc:00:00:01")
    reg.reserve(profile="p", count=1, holder="agent:x", ttl_seconds=3600)
    powered_off: list[str] = []
    reaper = ReservationReaper(registry=reg, power_off=powered_off.append)
    assert reaper.reap_once() == []
    assert powered_off == []


def test_reaper_continues_when_one_power_off_fails(tmp_path: Path):
    """A machine we can't power off is still released — the lease is gone,
    and a bad power-off must not stop the rest of the sweep."""
    reg = _registry(tmp_path)
    _pool_machine(reg, "aa:bb:cc:00:00:01")
    _pool_machine(reg, "aa:bb:cc:00:00:02")
    reg.reserve(profile="p", count=2, holder="agent:x", ttl_seconds=3600)
    _force_expire(reg, "aa:bb:cc:00:00:01")
    _force_expire(reg, "aa:bb:cc:00:00:02")

    seen: list[str] = []

    def flaky_power_off(mac: str) -> None:
        seen.append(mac)
        if mac == "aa:bb:cc:00:00:01":
            raise RuntimeError("PDU unreachable")

    reaper = ReservationReaper(registry=reg, power_off=flaky_power_off)
    freed = reaper.reap_once()

    assert sorted(freed) == ["aa:bb:cc:00:00:01", "aa:bb:cc:00:00:02"]
    assert sorted(seen) == ["aa:bb:cc:00:00:01", "aa:bb:cc:00:00:02"]
    # Both released despite the failure.
    assert reg.availability("aa:bb:cc:00:00:01") == AVAILABILITY_AVAILABLE
    assert reg.availability("aa:bb:cc:00:00:02") == AVAILABILITY_AVAILABLE
