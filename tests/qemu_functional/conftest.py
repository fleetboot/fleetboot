"""Skip cleanly if libvirt or tftpjail aren't available, and make tftpjail
importable for the test."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest


TFTPJAIL_ROOT = (
    Path(__file__).resolve().parent.parent.parent.parent / "tftpjail"
)

if not TFTPJAIL_ROOT.is_dir():
    pytest.skip(
        f"tftpjail not found at {TFTPJAIL_ROOT}; clone it next to fleetboot "
        "to run QEMU functional tests",
        allow_module_level=True,
    )

if shutil.which("virsh") is None:
    pytest.skip(
        "virsh not on PATH; install libvirt-clients to run QEMU functional tests",
        allow_module_level=True,
    )

if shutil.which("qemu-system-x86_64") is None:
    pytest.skip(
        "qemu-system-x86_64 not on PATH; install qemu-system-x86 to run "
        "QEMU functional tests",
        allow_module_level=True,
    )

sys.path.insert(0, str(TFTPJAIL_ROOT))
