"""Tests for the SQLite-backed MachineRegistry."""

from pathlib import Path

import pytest

from fleetboot.server.registry import MachineRegistry


@pytest.fixture
def registry(tmp_path: Path) -> MachineRegistry:
    return MachineRegistry(tmp_path / "machines.sqlite")


def test_enroll_and_lookup_round_trip(registry: MachineRegistry):
    machine = registry.enroll(
        mac="aa:bb:cc:dd:ee:ff",
        profile_name="student-lab",
        architecture="x86_64",
        platform="efi",
    )
    assert machine.mac == "aa:bb:cc:dd:ee:ff"
    assert machine.profile_name == "student-lab"
    assert machine.architecture == "x86_64"
    assert machine.platform == "efi"
    assert machine.created_at  # populated by DB default

    refreshed = registry.lookup("aa:bb:cc:dd:ee:ff")
    assert refreshed == machine


def test_lookup_unknown_returns_none(registry: MachineRegistry):
    assert registry.lookup("aa:bb:cc:dd:ee:00") is None


def test_mac_normalisation_on_enroll_and_lookup(registry: MachineRegistry):
    """Dashes, dots, and mixed case must all collapse to the canonical form."""
    registry.enroll(
        mac="AA-BB-CC-DD-EE-FF",
        profile_name="p",
        architecture="x86_64",
        platform="efi",
    )
    via_dots = registry.lookup("aa.bb.cc.dd.ee.ff")
    via_dashes = registry.lookup("AA-BB-CC-DD-EE-FF")
    via_colons = registry.lookup("aa:bb:cc:dd:ee:ff")
    assert via_dots and via_dashes and via_colons
    assert via_dots == via_dashes == via_colons
    assert via_colons.mac == "aa:bb:cc:dd:ee:ff"


def test_enroll_replaces_existing_row(registry: MachineRegistry):
    """Re-enrolling the same MAC should update, not duplicate."""
    registry.enroll(
        mac="aa:bb:cc:dd:ee:ff",
        profile_name="student",
        architecture="x86_64",
        platform="efi",
    )
    registry.enroll(
        mac="aa:bb:cc:dd:ee:ff",
        profile_name="teacher",
        architecture="x86_64",
        platform="efi",
    )
    rows = registry.list_all()
    assert len(rows) == 1
    assert rows[0].profile_name == "teacher"


def test_list_all_orders_by_created_then_mac(registry: MachineRegistry):
    registry.enroll(
        mac="aa:bb:cc:dd:ee:01",
        profile_name="p", architecture="x86_64", platform="efi",
    )
    registry.enroll(
        mac="aa:bb:cc:dd:ee:02",
        profile_name="p", architecture="x86_64", platform="efi",
    )
    macs = [m.mac for m in registry.list_all()]
    assert macs == ["aa:bb:cc:dd:ee:01", "aa:bb:cc:dd:ee:02"]


def test_remove_existing_returns_true(registry: MachineRegistry):
    registry.enroll(
        mac="aa:bb:cc:dd:ee:ff",
        profile_name="p", architecture="x86_64", platform="efi",
    )
    assert registry.remove("aa:bb:cc:dd:ee:ff") is True
    assert registry.lookup("aa:bb:cc:dd:ee:ff") is None


def test_remove_unknown_returns_false(registry: MachineRegistry):
    assert registry.remove("aa:bb:cc:dd:ee:ff") is False


def test_registry_persists_across_instances(tmp_path: Path):
    """A second registry pointing at the same file sees the same rows."""
    path = tmp_path / "fleet.sqlite"
    MachineRegistry(path).enroll(
        mac="aa:bb:cc:dd:ee:ff",
        profile_name="p",
        architecture="x86_64",
        platform="efi",
    )
    reopened = MachineRegistry(path)
    assert reopened.lookup("aa:bb:cc:dd:ee:ff") is not None
