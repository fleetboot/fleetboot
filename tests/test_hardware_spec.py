"""Tests for the hardware spec: the stable "what is this box made of"
record distilled from inventory reports and kept on the machine row."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from fleetboot.server.hardware_spec import merge_spec, spec_from_inventory
from fleetboot.server.registry import MachineRegistry


MAC = "aa:bb:cc:dd:ee:01"

CURRENT_INVENTORY = {
    "cpu_model": "Intel(R) Core(TM) i5-6500T CPU @ 2.50GHz",
    "cpu_count": 4,
    "cpu": {
        "model": "Intel(R) Core(TM) i5-6500T CPU @ 2.50GHz",
        "vendor": "GenuineIntel", "sockets": 1, "cores": 4, "threads": 4,
        "max_mhz": 3100,
    },
    "motherboard": {
        "system_vendor": "HP", "system_product": "HP ProDesk 400 G2 MINI",
        "board_name": "806A", "bios_version": "N23 Ver. 02.10",
    },
    "mem_total_mb": 15894,
    "mem_available_mb": 14000,
    "disks": [{
        "name": "nvme0n1", "size_bytes": 256060514304, "size_gb": 238,
        "connection": "nvme", "media": "ssd", "removable": False,
        "model": "PM981 NVMe Samsung 256GB", "firmware": "EXD71D1Q",
        "link_speed": "PCIe 5.0 GT/s x4",
    }],
    "mounts": [{"path": "/", "total_gb": 7, "free_gb": 6}],
    "usb_devices": ["046d:c31c Logitech, Inc. Keyboard K120"],
}

# What images built before the detailed collectors send.
OLDER_INVENTORY = {
    "cpu_model": "Intel(R) Celeron(R) J4125 CPU @ 2.00GHz",
    "cpu_count": 4,
    "mem_total_mb": 7821,
    "disks": [{"name": "mmcblk0", "size_gb": 58, "model": "", "removable": False}],
}


# ---- Distilling the spec ----------------------------------------------------


def test_spec_keeps_the_stable_facts_and_drops_the_live_ones():
    spec = spec_from_inventory(CURRENT_INVENTORY)

    assert spec == {
        "ram_total_mb": 15894,
        "cpu": CURRENT_INVENTORY["cpu"],
        "motherboard": CURRENT_INVENTORY["motherboard"],
        "disks": CURRENT_INVENTORY["disks"],
    }


def test_spec_understands_inventories_from_older_images():
    """Older reporters only knew a logical CPU count — that is threads,
    so it must not be passed off as physical cores."""
    spec = spec_from_inventory(OLDER_INVENTORY)

    assert spec["ram_total_mb"] == 7821
    assert spec["cpu"] == {
        "model": "Intel(R) Celeron(R) J4125 CPU @ 2.00GHz", "threads": 4,
    }
    assert "cores" not in spec["cpu"]
    assert "motherboard" not in spec
    assert spec["disks"] == OLDER_INVENTORY["disks"]


@pytest.mark.parametrize("not_an_inventory", [None, [], "text", {}])
def test_spec_from_something_unusable_is_empty(not_an_inventory):
    assert spec_from_inventory(not_an_inventory) == {}


def test_merge_replaces_only_the_sections_the_latest_report_carries():
    previous = {"ram_total_mb": 8000, "cpu": {"model": "old"}, "disks": []}

    merged = merge_spec(previous, {"ram_total_mb": 16000})

    assert merged == {"ram_total_mb": 16000, "cpu": {"model": "old"}, "disks": []}


# ---- On the machine record --------------------------------------------------


@pytest.fixture
def registry(tmp_path: Path) -> MachineRegistry:
    registry = MachineRegistry(tmp_path / "machines.sqlite")
    registry.enroll(mac=MAC, profile_name="default",
                    architecture="x86_64", platform="efi")
    return registry


def test_a_machine_with_no_report_has_no_spec(registry: MachineRegistry):
    machine = registry.lookup(MAC)
    assert machine.hardware_spec is None
    assert machine.ram_total_mb is None


def test_a_hardware_report_fills_in_the_machine_spec(registry: MachineRegistry):
    registry.update_hardware(MAC, json.dumps(CURRENT_INVENTORY))

    machine = registry.lookup(MAC)
    assert machine.ram_total_mb == 15894
    assert machine.hardware_spec["cpu"]["cores"] == 4
    assert machine.hardware_spec["motherboard"]["board_name"] == "806A"
    assert machine.hardware_spec["disks"][0]["connection"] == "nvme"


def test_a_patchy_report_does_not_blank_the_spec(registry: MachineRegistry):
    """One report that couldn't read the disks doesn't mean the disks
    have gone — the earlier sections stay."""
    registry.update_hardware(MAC, json.dumps(CURRENT_INVENTORY))
    registry.update_hardware(MAC, json.dumps({"mem_total_mb": 15894}))

    spec = registry.lookup(MAC).hardware_spec
    assert spec["disks"] == CURRENT_INVENTORY["disks"]
    assert spec["cpu"] == CURRENT_INVENTORY["cpu"]


def test_the_spec_survives_re_enrolment(registry: MachineRegistry):
    """Re-enrolment changes what the machine boots, not what it is."""
    registry.update_hardware(MAC, json.dumps(CURRENT_INVENTORY))

    registry.enroll(mac=MAC, profile_name="claude-compute",
                    architecture="x86_64", platform="efi")

    machine = registry.lookup(MAC)
    assert machine.profile_name == "claude-compute"
    assert machine.ram_total_mb == 15894
    assert machine.hardware_spec["cpu"]["threads"] == 4


def test_existing_machines_get_a_spec_from_their_stored_inventory(tmp_path: Path):
    """A database from before the spec column existed must come up with
    every machine's spec already filled in from the inventory it holds,
    rather than waiting for the next heartbeat."""
    path = tmp_path / "machines.sqlite"
    registry = MachineRegistry(path)
    registry.enroll(mac=MAC, profile_name="default",
                    architecture="x86_64", platform="efi")
    registry.update_hardware(MAC, json.dumps(OLDER_INVENTORY))
    with sqlite3.connect(path) as connection:
        connection.execute("ALTER TABLE machines DROP COLUMN hardware_spec")

    reopened = MachineRegistry(path)

    assert reopened.lookup(MAC).ram_total_mb == 7821
    assert reopened.lookup(MAC).hardware_spec["cpu"]["threads"] == 4
