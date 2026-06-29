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


def test_update_hostname_sets_value_and_timestamp(registry: MachineRegistry):
    registry.enroll(
        mac="aa:bb:cc:dd:ee:ff", profile_name="default",
        architecture="x86_64", platform="efi",
    )
    registry.update_hostname("aa:bb:cc:dd:ee:ff", "lab-pc-01")
    machine = registry.lookup("aa:bb:cc:dd:ee:ff")
    assert machine is not None
    assert machine.hostname == "lab-pc-01"
    assert machine.hostname_seen_at is not None


def test_update_hostname_is_noop_for_unknown_mac(registry: MachineRegistry):
    """We never want a status report to fail because the row hasn't landed."""
    registry.update_hostname("aa:bb:cc:dd:ee:ff", "lab-pc-01")  # no raise
    assert registry.lookup("aa:bb:cc:dd:ee:ff") is None


def test_update_hostname_ignores_whitespace_only_input(registry: MachineRegistry):
    registry.enroll(
        mac="aa:bb:cc:dd:ee:ff", profile_name="default",
        architecture="x86_64", platform="efi",
    )
    registry.update_hostname("aa:bb:cc:dd:ee:ff", "")
    registry.update_hostname("aa:bb:cc:dd:ee:ff", "   ")
    machine = registry.lookup("aa:bb:cc:dd:ee:ff")
    assert machine is not None
    assert machine.hostname is None


def test_update_diagnostics_persists(registry: MachineRegistry):
    registry.enroll(
        mac="aa:bb:cc:dd:ee:ff", profile_name="default",
        architecture="x86_64", platform="efi",
    )
    body = "# systemctl --failed\nfoo.service\n\n# display-manager.service: active"
    registry.update_diagnostics("aa:bb:cc:dd:ee:ff", body)
    m = registry.lookup("aa:bb:cc:dd:ee:ff")
    assert m is not None
    assert m.last_diagnostics == body
    assert m.last_diagnostics_at is not None


def test_update_diagnostics_skips_empty(registry: MachineRegistry):
    registry.enroll(
        mac="aa:bb:cc:dd:ee:ff", profile_name="default",
        architecture="x86_64", platform="efi",
    )
    registry.update_diagnostics("aa:bb:cc:dd:ee:ff", "")
    registry.update_diagnostics("aa:bb:cc:dd:ee:ff", "   \n  ")
    m = registry.lookup("aa:bb:cc:dd:ee:ff")
    assert m is not None
    assert m.last_diagnostics is None


def test_update_boot_version_persists(registry: MachineRegistry):
    registry.enroll(
        mac="aa:bb:cc:dd:ee:ff", profile_name="default",
        architecture="x86_64", platform="efi",
    )
    registry.update_boot_version("aa:bb:cc:dd:ee:ff", "2026-06-28T22:00:00Z")
    machine = registry.lookup("aa:bb:cc:dd:ee:ff")
    assert machine is not None
    assert machine.boot_version == "2026-06-28T22:00:00Z"
    assert machine.boot_version_seen_at is not None


def test_update_boot_version_is_noop_for_unknown_mac(registry: MachineRegistry):
    registry.update_boot_version("aa:bb:cc:dd:ee:ff", "vX")  # no raise
    assert registry.lookup("aa:bb:cc:dd:ee:ff") is None


def test_enroll_stores_scratch_mode_default_volatile(registry: MachineRegistry):
    m = registry.enroll(
        mac="aa:bb:cc:dd:ee:01", profile_name="default",
        architecture="x86_64", platform="efi",
    )
    assert m.scratch_mode == "volatile"


def test_enroll_stores_scratch_mode_persistent(registry: MachineRegistry):
    m = registry.enroll(
        mac="aa:bb:cc:dd:ee:02", profile_name="default",
        architecture="x86_64", platform="efi",
        scratch_mode="persistent",
    )
    assert m.scratch_mode == "persistent"


def test_enroll_rejects_unknown_scratch_mode(registry: MachineRegistry):
    import pytest
    with pytest.raises(ValueError):
        registry.enroll(
            mac="aa:bb:cc:dd:ee:03", profile_name="default",
            architecture="x86_64", platform="efi",
            scratch_mode="wipe-on-friday",
        )


def test_enroll_tracks_provenance(registry: MachineRegistry):
    """enrolled_by defaults to 'manual'; passing rule:<name> keeps it."""
    m1 = registry.enroll(
        mac="aa:bb:cc:dd:ee:01", profile_name="default",
        architecture="x86_64", platform="efi",
    )
    assert m1.enrolled_by == "manual"
    m2 = registry.enroll(
        mac="aa:bb:cc:dd:ee:02", profile_name="default",
        architecture="x86_64", platform="efi",
        enrolled_by="rule:vbox",
    )
    assert m2.enrolled_by == "rule:vbox"


def test_log_and_list_boot_events(registry: MachineRegistry):
    registry.log_boot_event(mac="aa:bb:cc:dd:ee:ff", state="network_up")
    registry.log_boot_event(
        mac="aa:bb:cc:dd:ee:ff", state="user_logged_in", detail="alice"
    )
    events = registry.recent_boot_events()
    assert len(events) == 2
    # Newest first.
    assert events[0].state == "user_logged_in"
    assert events[0].detail == "alice"
    assert events[1].state == "network_up"
    assert events[1].detail is None


def test_recent_boot_events_filters_by_mac(registry: MachineRegistry):
    registry.log_boot_event(mac="aa:bb:cc:dd:ee:01", state="network_up")
    registry.log_boot_event(mac="aa:bb:cc:dd:ee:02", state="network_up")
    only_one = registry.recent_boot_events(mac="aa:bb:cc:dd:ee:01")
    assert len(only_one) == 1
    assert only_one[0].mac == "aa:bb:cc:dd:ee:01"


def test_boot_events_normalise_mac_on_log(registry: MachineRegistry):
    registry.log_boot_event(mac="AA-BB-CC-DD-EE-FF", state="network_up")
    events = registry.recent_boot_events(mac="aa:bb:cc:dd:ee:ff")
    assert len(events) == 1
    assert events[0].mac == "aa:bb:cc:dd:ee:ff"


def test_boot_events_respect_limit(registry: MachineRegistry):
    for i in range(50):
        registry.log_boot_event(
            mac="aa:bb:cc:dd:ee:ff", state=f"state-{i}"
        )
    assert len(registry.recent_boot_events(limit=10)) == 10


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
