"""Make the sister tftpjail project importable and skip cleanly if
VirtualBox isn't available."""

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
        "to run VBox functional tests",
        allow_module_level=True,
    )

if shutil.which("VBoxManage") is None:
    pytest.skip(
        "VBoxManage not on PATH; install VirtualBox to run VBox functional tests",
        allow_module_level=True,
    )

sys.path.insert(0, str(TFTPJAIL_ROOT))
