"""Tests for the auto-enrol rules: registry layer, /resolve integration,
and the admin CRUD API."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from fleetboot.server.app import create_app
from fleetboot.server.boot_sessions import BootSessionStore
from fleetboot.server.registry import AutoEnrolRule, MachineRegistry


MINT_SECRET = "mint-secret"
ADMIN_SECRET = "admin-secret"


@pytest.fixture
def registry(tmp_path: Path) -> MachineRegistry:
    return MachineRegistry(tmp_path / "machines.sqlite")


# ---- Registry layer -------------------------------------------------------


def test_add_and_list_mac_prefix_rule(registry: MachineRegistry):
    rule = registry.add_auto_enrol_rule(
        name="vbox", match_kind="mac_prefix", match_value="08:00:27",
        profile_name="school",
    )
    assert isinstance(rule, AutoEnrolRule)
    assert rule.name == "vbox"
    assert rule.match_value == "08:00:27"
    listed = registry.list_auto_enrol_rules()
    assert len(listed) == 1
    assert listed[0].id == rule.id


def test_mac_prefix_match_normalises_input(registry: MachineRegistry):
    """`08-00-27` should match the same MACs as `08:00:27`."""
    registry.add_auto_enrol_rule(
        name="vbox", match_kind="mac_prefix",
        match_value="08-00-27", profile_name="school",
    )
    match = registry.find_matching_rule("08:00:27:aa:bb:cc")
    assert match is not None
    assert match.name == "vbox"


def test_empty_mac_prefix_catches_all_unknown_macs(registry: MachineRegistry):
    """A catch-all rule with empty match_value matches every MAC."""
    registry.add_auto_enrol_rule(
        name="catch-all", match_kind="mac_prefix",
        match_value="", profile_name="default",
    )
    assert registry.find_matching_rule("aa:bb:cc:dd:ee:ff") is not None
    assert registry.find_matching_rule("52:54:00:11:22:33") is not None


def test_ip_cidr_rule_matches_within_subnet(registry: MachineRegistry):
    registry.add_auto_enrol_rule(
        name="student-lan", match_kind="ip_cidr",
        match_value="192.168.99.0/24", profile_name="school",
    )
    assert registry.find_matching_rule(
        "aa:bb:cc:dd:ee:ff", source_ip="192.168.99.10",
    ) is not None
    # A MAC with no source IP can't match an IP-based rule.
    assert registry.find_matching_rule(
        "aa:bb:cc:dd:ee:ff", source_ip=None,
    ) is None
    # IP outside the subnet doesn't match.
    assert registry.find_matching_rule(
        "aa:bb:cc:dd:ee:ff", source_ip="10.0.0.1",
    ) is None


def test_first_matching_rule_wins(registry: MachineRegistry):
    """Lowest ID matches first; later rules with the same kind don't fire."""
    registry.add_auto_enrol_rule(
        name="first", match_kind="mac_prefix",
        match_value="08", profile_name="default",
    )
    registry.add_auto_enrol_rule(
        name="second", match_kind="mac_prefix",
        match_value="08", profile_name="school",
    )
    match = registry.find_matching_rule("08:00:27:aa:bb:cc")
    assert match is not None
    assert match.name == "first"


def test_rule_platform_any_matches_both_efi_and_pc(registry: MachineRegistry):
    """The default behaviour — a rule with platform='any' fires
    regardless of what the URL says."""
    registry.add_auto_enrol_rule(
        name="catch-all", match_kind="mac_prefix",
        match_value="08", profile_name="default", platform="any",
    )
    efi_match = registry.find_matching_rule("08:11:22:33:44:55", platform="efi")
    pc_match = registry.find_matching_rule("08:11:22:33:44:55", platform="pc")
    assert efi_match is not None and efi_match.name == "catch-all"
    assert pc_match is not None and pc_match.name == "catch-all"


def test_rule_platform_efi_only_matches_efi(registry: MachineRegistry):
    registry.add_auto_enrol_rule(
        name="uefi-rule", match_kind="mac_prefix",
        match_value="08", profile_name="default", platform="efi",
    )
    assert registry.find_matching_rule(
        "08:11:22:33:44:55", platform="efi"
    ) is not None
    # A BIOS URL must NOT match a UEFI-gated rule.
    assert registry.find_matching_rule(
        "08:11:22:33:44:55", platform="pc"
    ) is None


def test_per_platform_rules_in_same_subnet(registry: MachineRegistry):
    """Two rules with the same predicate but different platform gates
    each fire for their own client class."""
    registry.add_auto_enrol_rule(
        name="lab-uefi", match_kind="ip_cidr",
        match_value="192.168.25.0/24", profile_name="cinnamon-desktop",
        platform="efi", serial_console=False,
    )
    registry.add_auto_enrol_rule(
        name="lab-bios", match_kind="ip_cidr",
        match_value="192.168.25.0/24", profile_name="cinnamon-desktop",
        platform="pc", serial_console=True,
    )
    uefi = registry.find_matching_rule(
        "aa:bb:cc:dd:ee:ff", source_ip="192.168.25.10", platform="efi"
    )
    bios = registry.find_matching_rule(
        "aa:bb:cc:dd:ee:ff", source_ip="192.168.25.10", platform="pc"
    )
    assert uefi is not None and uefi.name == "lab-uefi"
    assert bios is not None and bios.name == "lab-bios"
    # The serial-console differentiator is preserved per platform.
    assert uefi.serial_console is False
    assert bios.serial_console is True


def test_rule_platform_specific_skipped_when_url_omits_platform(
    registry: MachineRegistry,
):
    """A platform-gated rule must not fire if we don't know the platform —
    safer than guessing wrong."""
    registry.add_auto_enrol_rule(
        name="uefi-rule", match_kind="mac_prefix",
        match_value="08", profile_name="default", platform="efi",
    )
    assert registry.find_matching_rule(
        "08:11:22:33:44:55", platform=None
    ) is None


def test_remove_auto_enrol_rule(registry: MachineRegistry):
    rule = registry.add_auto_enrol_rule(
        name="vbox", match_kind="mac_prefix",
        match_value="08:00:27", profile_name="school",
    )
    assert registry.remove_auto_enrol_rule(rule.id) is True
    assert registry.remove_auto_enrol_rule(rule.id) is False  # idempotent-ish
    assert registry.list_auto_enrol_rules() == []


# ---- /resolve auto-enrol integration -------------------------------------


def _resolve_client(registry: MachineRegistry) -> TestClient:
    app = create_app(
        registry=registry,
        mint_secret=MINT_SECRET,
        admin_secret=ADMIN_SECRET,
        sessions=BootSessionStore(),
    )
    return TestClient(app)


def test_resolve_auto_enrols_on_first_hit(registry: MachineRegistry):
    """An unknown MAC that matches a rule is enrolled by /resolve."""
    registry.add_auto_enrol_rule(
        name="vbox", match_kind="mac_prefix",
        match_value="08:00:27", profile_name="school",
    )
    client = _resolve_client(registry)
    response = client.get(
        "/resolve/08:00:27:aa:bb:cc",
        headers={"Authorization": f"Bearer {MINT_SECRET}"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["mac"] == "08:00:27:aa:bb:cc"
    assert body["profile_name"] == "school"
    assert body["enrolled_by"] == "rule:vbox"
    # And the row is now in the registry — a follow-up lookup hits the
    # cached row, not the rule again.
    assert registry.lookup("08:00:27:aa:bb:cc") is not None


def test_resolve_returns_404_when_no_rule_matches(registry: MachineRegistry):
    """Without any rule and without a row, /resolve still 404s."""
    client = _resolve_client(registry)
    response = client.get(
        "/resolve/aa:bb:cc:dd:ee:ff",
        headers={"Authorization": f"Bearer {MINT_SECRET}"},
    )
    assert response.status_code == 404


def test_resolve_passes_source_ip_to_rule_matching(registry: MachineRegistry):
    registry.add_auto_enrol_rule(
        name="student-lan", match_kind="ip_cidr",
        match_value="192.168.99.0/24", profile_name="school",
    )
    client = _resolve_client(registry)
    # Without source_ip, IP rule never fires.
    no_ip = client.get(
        "/resolve/aa:bb:cc:dd:ee:ff",
        headers={"Authorization": f"Bearer {MINT_SECRET}"},
    )
    assert no_ip.status_code == 404
    # With source_ip in the subnet, rule fires and enrols.
    with_ip = client.get(
        "/resolve/aa:bb:cc:dd:ee:ff?source_ip=192.168.99.42",
        headers={"Authorization": f"Bearer {MINT_SECRET}"},
    )
    assert with_ip.status_code == 200
    assert with_ip.json()["enrolled_by"] == "rule:student-lan"


# ---- /auto-enrol-rules admin CRUD ----------------------------------------


def test_auto_enrol_rules_crud_requires_admin(registry: MachineRegistry):
    client = _resolve_client(registry)
    # No auth
    assert client.get("/auto-enrol-rules").status_code == 401
    # Wrong secret
    bad = client.get(
        "/auto-enrol-rules", headers={"Authorization": "Bearer nope"},
    )
    assert bad.status_code == 401
    # Mint-secret is for /resolve, not /auto-enrol-rules — admin-only.
    wrong_realm = client.get(
        "/auto-enrol-rules",
        headers={"Authorization": f"Bearer {MINT_SECRET}"},
    )
    assert wrong_realm.status_code == 401


def test_auto_enrol_rules_create_list_delete(registry: MachineRegistry):
    client = _resolve_client(registry)
    auth = {"Authorization": f"Bearer {ADMIN_SECRET}"}

    create = client.post(
        "/auto-enrol-rules",
        json={
            "name": "vbox",
            "match_kind": "mac_prefix",
            "match_value": "08:00:27",
            "profile_name": "school",
        },
        headers=auth,
    )
    assert create.status_code == 201
    rule_id = create.json()["id"]

    listed = client.get("/auto-enrol-rules", headers=auth)
    assert listed.status_code == 200
    assert any(r["id"] == rule_id for r in listed.json())

    deleted = client.delete(f"/auto-enrol-rules/{rule_id}", headers=auth)
    assert deleted.status_code == 204
    assert client.get("/auto-enrol-rules", headers=auth).json() == []


def test_auto_enrol_rule_rejects_unknown_match_kind(registry: MachineRegistry):
    client = _resolve_client(registry)
    response = client.post(
        "/auto-enrol-rules",
        json={
            "name": "x", "match_kind": "subnet_mask",
            "match_value": "ignored", "profile_name": "default",
        },
        headers={"Authorization": f"Bearer {ADMIN_SECRET}"},
    )
    assert response.status_code == 400
