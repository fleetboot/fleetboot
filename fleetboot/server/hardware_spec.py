"""A machine's hardware spec: what the box is made of, distilled from its
reporter's inventory.

The inventory is a live snapshot (free memory, free disk, what's plugged
in right now). The spec is the stable part — RAM, CPU, motherboard,
disks — kept on the machine record so it reads like the machine's
architecture does: present even after a re-enrolment, and not lost
because one report came back patchy.

Both inventory shapes are understood: the current reporter sends `cpu`
and `motherboard` sections and detailed disks; images built before that
send only `cpu_model`, `cpu_count` and basic disks.
"""

from __future__ import annotations

from typing import Optional


# The facts a disk keeps in the spec, in the order they read best.
_DISK_FIELDS = (
    "name", "size_bytes", "size_gb", "vendor", "model", "firmware",
    "connection", "media", "link_speed", "removable",
)

SPEC_SECTIONS = ("ram_total_mb", "cpu", "motherboard", "disks")


def spec_from_inventory(inventory: object) -> dict:
    """The spec sections this inventory can tell us about. A section the
    inventory doesn't cover is simply absent, never empty."""
    if not isinstance(inventory, dict):
        return {}
    spec: dict = {}

    ram_total_mb = inventory.get("mem_total_mb")
    if _is_positive_whole_number(ram_total_mb):
        spec["ram_total_mb"] = ram_total_mb

    cpu = _cpu_spec(inventory)
    if cpu:
        spec["cpu"] = cpu

    motherboard = inventory.get("motherboard")
    if isinstance(motherboard, dict) and motherboard:
        spec["motherboard"] = {
            key: value for key, value in motherboard.items()
            if isinstance(value, str) and value
        }

    disks = inventory.get("disks")
    if isinstance(disks, list):
        spec["disks"] = [
            {key: disk[key] for key in _DISK_FIELDS if key in disk}
            for disk in disks if isinstance(disk, dict)
        ]
    return spec


def merge_spec(previous: Optional[dict], latest: dict) -> dict:
    """Section by section, the latest report wins where it has something
    to say; anything it left out is kept from before."""
    merged = dict(previous or {})
    merged.update(latest)
    return merged


def _cpu_spec(inventory: dict) -> dict:
    cpu = inventory.get("cpu")
    if isinstance(cpu, dict) and cpu:
        return dict(cpu)
    # Older reporters: a model string and a logical CPU count, which is
    # hardware threads — physical cores were never reported.
    older: dict = {}
    if isinstance(inventory.get("cpu_model"), str) and inventory["cpu_model"]:
        older["model"] = inventory["cpu_model"]
    if _is_positive_whole_number(inventory.get("cpu_count")):
        older["threads"] = inventory["cpu_count"]
    return older


def _is_positive_whole_number(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0
