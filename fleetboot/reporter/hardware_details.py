"""Detailed hardware facts for the reporter's inventory: CPU, motherboard, disks.

Everything here is read from the kernel's own views (/sys, /proc) plus
`lscpu`, so no extra packages are needed in the image. Each collector
takes the paths it reads as arguments so tests can point it at a fake
tree, and each one is best-effort: a missing file leaves that one fact
out rather than failing the report.

Serial numbers are deliberately not collected — they identify a
specific box to anyone who can read the inventory, and nothing in the
fleet needs them.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional


# ---- CPU ------------------------------------------------------------------


def cpu_details(lscpu_output: str, cpuinfo_text: str) -> dict:
    """Model, sockets, physical cores, hardware threads and top speed.

    `lscpu` is the primary source because it knows how to name arm64
    cores (e.g. "Cortex-A72"), which /proc/cpuinfo on arm does not.
    /proc/cpuinfo fills in the model on the rare kernel where lscpu
    printed nothing.
    """
    fields: dict[str, str] = {}
    for line in lscpu_output.splitlines():
        key, separator, value = line.partition(":")
        if separator:
            # lscpu repeats some keys per cluster on big.LITTLE arm
            # chips; the first one describes the boot cluster.
            fields.setdefault(key.strip(), value.strip())

    details: dict = {}
    model = fields.get("Model name") or _cpuinfo_model(cpuinfo_text)
    if model:
        details["model"] = model
    if fields.get("Vendor ID"):
        details["vendor"] = fields["Vendor ID"]

    threads = _as_int(fields.get("CPU(s)"))
    sockets = _as_int(fields.get("Socket(s)"))
    cores_per_socket = _as_int(fields.get("Core(s) per socket"))
    if sockets is None:
        # arm64 lscpu reports clusters instead of sockets.
        sockets = _as_int(fields.get("Cluster(s)"))
    if threads is not None:
        details["threads"] = threads
    if sockets is not None:
        details["sockets"] = sockets
    if sockets is not None and cores_per_socket is not None:
        details["cores"] = sockets * cores_per_socket
    maximum_mhz = _as_float(fields.get("CPU max MHz"))
    if maximum_mhz is not None:
        details["max_mhz"] = round(maximum_mhz)
    return details


def _cpuinfo_model(cpuinfo_text: str) -> Optional[str]:
    for line in cpuinfo_text.splitlines():
        if line.startswith("model name"):
            return line.split(":", 1)[1].strip() or None
    return None


# ---- Motherboard ----------------------------------------------------------


# DMI file → the name it is reported under. Serial and asset-tag files
# are left out on purpose (see the module docstring).
_MOTHERBOARD_FIELDS = (
    ("sys_vendor", "system_vendor"),
    ("product_name", "system_product"),
    ("product_version", "system_version"),
    ("board_vendor", "board_vendor"),
    ("board_name", "board_name"),
    ("board_version", "board_version"),
    ("bios_vendor", "bios_vendor"),
    ("bios_version", "bios_version"),
    ("bios_date", "bios_date"),
)

# Placeholder strings firmware vendors leave in DMI fields they never
# filled in. Reporting them would only look like information.
_DMI_PLACEHOLDERS = frozenset({
    "", "default string", "to be filled by o.e.m.", "not applicable",
    "not specified", "system product name", "system manufacturer",
    "system version", "none", "0", "0123456789", "x.x",
})


def motherboard_details(dmi_directory: Path, device_tree_model: Path) -> dict:
    """System, board and firmware identity from DMI.

    x86 machines (and arm64 servers with SMBIOS) publish this under
    /sys/class/dmi/id. Most arm64 boards have no DMI but name
    themselves in the device tree instead, which becomes the system
    product.
    """
    details: dict = {}
    for file_name, reported_name in _MOTHERBOARD_FIELDS:
        value = _read_text(dmi_directory / file_name)
        if value and value.lower() not in _DMI_PLACEHOLDERS:
            details[reported_name] = value
    if "system_product" not in details:
        model = _read_text(device_tree_model)
        if model:
            # The device tree string is NUL-terminated.
            details["system_product"] = model.rstrip("\x00").strip()
    return details


# ---- Disks ----------------------------------------------------------------


# Block devices that are not physical disks.
_NOT_A_DISK_PREFIXES = ("loop", "ram", "sr", "fd", "dm-", "zram", "md", "nbd")


def disk_details(system_root: Path) -> list[dict]:
    """Every physical disk: size, identity, how it is attached, and speed.

    `system_root` is normally /sys. Each disk reports:
      - name, size_bytes, size_gb (GiB, kept for older dashboards)
      - vendor, model, firmware — enough to look the exact drive up
      - connection: nvme | sata | usb | sas_or_scsi | mmc | virtio
      - media: ssd | hdd | flash | virtual
      - link_speed: the negotiated link, e.g. "6.0 Gbps" (SATA),
        "PCIe 8.0 GT/s x4" (NVMe) or "USB 5000 Mbps"
      - removable
    """
    block_directory = system_root / "block"
    disks: list[dict] = []
    try:
        names = sorted(entry.name for entry in block_directory.iterdir())
    except OSError:
        return disks
    for name in names:
        if name.startswith(_NOT_A_DISK_PREFIXES):
            continue
        disk_directory = block_directory / name
        sectors = _as_int(_read_text(disk_directory / "size"))
        if sectors is None:
            continue
        # The real device path says what bus the disk hangs off.
        device_path = os.path.realpath(disk_directory)
        connection = _connection(name, device_path)
        disk: dict = {
            "name": name,
            "size_bytes": sectors * 512,
            "size_gb": (sectors * 512) // (1024 ** 3),
            "connection": connection,
            "media": _media(connection, disk_directory),
            "removable": _read_text(disk_directory / "removable") == "1",
        }
        device_directory = disk_directory / "device"
        for file_name, reported_name in (
            ("vendor", "vendor"),
            ("model", "model"),
            ("rev", "firmware"),           # SATA / SCSI / USB
            ("firmware_rev", "firmware"),  # NVMe
        ):
            value = _read_text(device_directory / file_name)
            if value and reported_name not in disk:
                disk[reported_name] = value
        # A SATA disk's vendor field is just the bus name, not a maker.
        if disk.get("vendor", "").upper() == "ATA":
            del disk["vendor"]
        disk.setdefault("model", "")
        link_speed = _link_speed(connection, device_path, system_root)
        if link_speed:
            disk["link_speed"] = link_speed
        disks.append(disk)
    return disks


def _connection(name: str, device_path: str) -> str:
    # USB first: a USB-to-SATA bridge also shows SCSI parts in its path.
    if "/usb" in device_path:
        return "usb"
    if name.startswith("nvme"):
        return "nvme"
    if name.startswith("mmcblk"):
        return "mmc"
    if name.startswith("vd") or "/virtio" in device_path:
        return "virtio"
    if re.search(r"/ata\d+/", device_path):
        return "sata"
    return "sas_or_scsi"


def _media(connection: str, disk_directory: Path) -> str:
    if connection == "nvme":
        return "ssd"
    if connection == "mmc":
        return "flash"
    if connection == "virtio":
        return "virtual"
    rotational = _read_text(disk_directory / "queue" / "rotational")
    return "hdd" if rotational == "1" else "ssd"


def _link_speed(connection: str, device_path: str, system_root: Path) -> Optional[str]:
    if connection == "sata":
        port = re.search(r"/ata(\d+)/", device_path)
        if port:
            return _read_text(
                system_root / "class" / "ata_link" / f"link{port.group(1)}"
                / "sata_spd"
            )
    if connection == "nvme":
        # .../<pci device>/nvme/nvme0/nvme0n1 — the PCI device is three up.
        pci_device = Path(device_path).parent.parent.parent
        speed = _read_text(pci_device / "current_link_speed")
        width = _read_text(pci_device / "current_link_width")
        if speed:
            return f"PCIe {speed.replace(' PCIe', '')}" + (f" x{width}" if width else "")
    if connection == "usb":
        # Walk up to the USB device itself, which carries its speed.
        for parent in Path(device_path).parents:
            if (parent / "idVendor").exists():
                speed = _read_text(parent / "speed")
                return f"USB {speed} Mbps" if speed else None
    return None


# ---- Small helpers --------------------------------------------------------


def _read_text(path: Path) -> Optional[str]:
    try:
        return path.read_text(errors="replace").strip() or None
    except OSError:
        return None


def _as_int(value: Optional[str]) -> Optional[int]:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def _as_float(value: Optional[str]) -> Optional[float]:
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None
