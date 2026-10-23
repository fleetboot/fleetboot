"""Tests for the reporter's detailed hardware collectors: CPU, motherboard,
disks. Each collector reads from paths it is given, so these build small
fake /sys and DMI trees rather than reading the test host's hardware."""

from __future__ import annotations

import os
from pathlib import Path

from fleetboot.reporter.hardware_details import (
    cpu_details,
    disk_details,
    motherboard_details,
)


# ---- CPU ------------------------------------------------------------------


X86_LSCPU = """\
Architecture:             x86_64
CPU(s):                   8
Vendor ID:                GenuineIntel
  Model name:             Intel(R) Core(TM) i7-7700 CPU @ 3.60GHz
    Thread(s) per core:   2
    Core(s) per socket:   4
    Socket(s):            1
    CPU max MHz:          4200.0000
"""

ARM64_LSCPU = """\
Architecture:             aarch64
CPU(s):                   4
Vendor ID:                ARM
  Model name:             Cortex-A72
    Thread(s) per core:   1
    Core(s) per cluster:  4
    Socket(s):            -
    Cluster(s):           1
    CPU max MHz:          1800.0000
"""


def test_cpu_details_separates_physical_cores_from_threads():
    """A hyperthreaded chip has twice as many threads as cores; an agent
    sizing a build needs both, not one ambiguous count."""
    assert cpu_details(X86_LSCPU, "") == {
        "model": "Intel(R) Core(TM) i7-7700 CPU @ 3.60GHz",
        "vendor": "GenuineIntel",
        "threads": 8,
        "sockets": 1,
        "cores": 4,
        "max_mhz": 4200,
    }


def test_cpu_details_names_arm64_cores_from_lscpu():
    """/proc/cpuinfo on arm64 has no model name; lscpu does, and arm64
    reports clusters where x86 reports sockets."""
    details = cpu_details(ARM64_LSCPU, "processor : 0\nBogoMIPS : 108.00\n")
    assert details["model"] == "Cortex-A72"
    assert details["threads"] == 4
    assert details["max_mhz"] == 1800


def test_cpu_details_falls_back_to_cpuinfo_for_the_model():
    details = cpu_details("", "processor\t: 0\nmodel name\t: AMD Ryzen 5 3600\n")
    assert details == {"model": "AMD Ryzen 5 3600"}


# ---- Motherboard ----------------------------------------------------------


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def test_motherboard_details_reads_dmi_and_drops_placeholders(tmp_path: Path):
    dmi = tmp_path / "dmi"
    _write(dmi / "sys_vendor", "HP\n")
    _write(dmi / "product_name", "HP ProDesk 400 G2 MINI\n")
    _write(dmi / "product_version", "To Be Filled By O.E.M.\n")
    _write(dmi / "board_vendor", "HP\n")
    _write(dmi / "board_name", "806A\n")
    _write(dmi / "bios_version", "N23 Ver. 02.10\n")
    _write(dmi / "bios_date", "11/01/2016\n")
    # Serial numbers identify one specific box and must never be sent.
    _write(dmi / "board_serial", "SECRET-SERIAL\n")
    _write(dmi / "product_serial", "SECRET-SERIAL\n")

    details = motherboard_details(dmi, tmp_path / "no-device-tree")

    assert details == {
        "system_vendor": "HP",
        "system_product": "HP ProDesk 400 G2 MINI",
        "board_vendor": "HP",
        "board_name": "806A",
        "bios_version": "N23 Ver. 02.10",
        "bios_date": "11/01/2016",
    }
    assert "SECRET-SERIAL" not in repr(details)


def test_motherboard_details_uses_the_device_tree_on_arm_boards(tmp_path: Path):
    device_tree_model = tmp_path / "model"
    device_tree_model.write_text("Raspberry Pi 4 Model B Rev 1.4\x00")

    details = motherboard_details(tmp_path / "no-dmi", device_tree_model)

    assert details == {"system_product": "Raspberry Pi 4 Model B Rev 1.4"}


# ---- Disks ----------------------------------------------------------------


def _fake_disk(
    system: Path, name: str, device_path: str, *, sectors: int,
    rotational: str = "0", removable: str = "0",
) -> Path:
    """Make /sys/block/<name> a symlink into /sys/devices, as the kernel
    does, and return the real disk directory."""
    real_directory = system / "devices" / device_path / name
    _write(real_directory / "size", f"{sectors}\n")
    _write(real_directory / "removable", f"{removable}\n")
    _write(real_directory / "queue" / "rotational", f"{rotational}\n")
    (system / "block").mkdir(parents=True, exist_ok=True)
    os.symlink(real_directory, system / "block" / name)
    return real_directory


def test_disk_details_reports_an_nvme_drive_and_its_pcie_link(tmp_path: Path):
    system = tmp_path / "sys"
    pci_device = "pci0000:00/0000:00:1d.0/0000:02:00.0"
    disk = _fake_disk(
        system, "nvme0n1", f"{pci_device}/nvme/nvme0", sectors=500118192,
    )
    controller = disk.parent
    _write(controller / "model", "Samsung SSD 970 EVO Plus 250GB\n")
    _write(controller / "firmware_rev", "2B2QEXM7\n")
    os.symlink(controller, disk / "device")
    _write(system / "devices" / pci_device / "current_link_speed", "8.0 GT/s PCIe\n")
    _write(system / "devices" / pci_device / "current_link_width", "4\n")

    assert disk_details(system) == [{
        "name": "nvme0n1",
        "size_bytes": 256060514304,
        "size_gb": 238,
        "connection": "nvme",
        "media": "ssd",
        "removable": False,
        "model": "Samsung SSD 970 EVO Plus 250GB",
        "firmware": "2B2QEXM7",
        "link_speed": "PCIe 8.0 GT/s x4",
    }]


def test_disk_details_reports_a_sata_hard_drive_and_its_link(tmp_path: Path):
    system = tmp_path / "sys"
    scsi_device = "pci0000:00/0000:00:17.0/ata3/host2/target2:0:0/2:0:0:0"
    disk = _fake_disk(
        system, "sda", f"{scsi_device}/block", sectors=1953525168,
        rotational="1",
    )
    device = system / "devices" / scsi_device
    # SATA disks report the bus name as their vendor — not useful.
    _write(device / "vendor", "ATA     \n")
    _write(device / "model", "WDC WD10EZEX-08W\n")
    _write(device / "rev", "1A01\n")
    os.symlink(device, disk / "device")
    _write(system / "class" / "ata_link" / "link3" / "sata_spd", "6.0 Gbps\n")

    [sata_disk] = disk_details(system)

    assert sata_disk["connection"] == "sata"
    assert sata_disk["media"] == "hdd"
    assert sata_disk["link_speed"] == "6.0 Gbps"
    assert sata_disk["model"] == "WDC WD10EZEX-08W"
    assert sata_disk["firmware"] == "1A01"
    assert "vendor" not in sata_disk
    assert sata_disk["size_bytes"] == 1953525168 * 512


def test_disk_details_reports_a_usb_drive_and_its_usb_speed(tmp_path: Path):
    """A USB stick also looks like a SCSI disk, so USB must win."""
    system = tmp_path / "sys"
    usb_device = "pci0000:00/0000:00:14.0/usb2/2-1"
    scsi_device = f"{usb_device}/2-1:1.0/host4/target4:0:0/4:0:0:0"
    disk = _fake_disk(
        system, "sdb", f"{scsi_device}/block", sectors=60062500,
        removable="1",
    )
    _write(system / "devices" / usb_device / "idVendor", "0781\n")
    _write(system / "devices" / usb_device / "speed", "5000\n")
    _write(system / "devices" / scsi_device / "vendor", "SanDisk \n")
    _write(system / "devices" / scsi_device / "model", "Ultra\n")
    os.symlink(system / "devices" / scsi_device, disk / "device")

    [usb_disk] = disk_details(system)

    assert usb_disk["connection"] == "usb"
    assert usb_disk["link_speed"] == "USB 5000 Mbps"
    assert usb_disk["vendor"] == "SanDisk"
    assert usb_disk["removable"] is True


def test_disk_details_skips_devices_that_are_not_disks(tmp_path: Path):
    system = tmp_path / "sys"
    for name in ("loop0", "zram0", "dm-0", "sr0", "md0"):
        _fake_disk(system, name, "virtual/block", sectors=1000)

    assert disk_details(system) == []


def test_disk_details_with_no_sysfs_reports_nothing(tmp_path: Path):
    assert disk_details(tmp_path / "missing") == []
