"""Make the sister tftpjail project importable from these tests."""

from __future__ import annotations

import sys
from pathlib import Path


# tftpjail lives at /home/matt/git/tftpjail, a sibling of fleetboot. Resolve
# it relative to this file rather than hard-coding the user's path.
TFTPJAIL_ROOT = (
    Path(__file__).resolve().parent.parent.parent.parent / "tftpjail"
)

if not TFTPJAIL_ROOT.is_dir():
    import pytest

    pytest.skip(
        f"tftpjail not found at {TFTPJAIL_ROOT}; clone it next to fleetboot "
        "to run functional tests",
        allow_module_level=True,
    )

sys.path.insert(0, str(TFTPJAIL_ROOT))
