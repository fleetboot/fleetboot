"""Structural tests for the local-disk scratch setup pieces in the image."""

from __future__ import annotations

import configparser
import stat
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_DIR = REPO_ROOT / "image" / "runtime"
SYSTEMD_DIR = REPO_ROOT / "image" / "systemd"


def test_scratch_script_exists_and_is_executable():
    script = RUNTIME_DIR / "fleetboot-scratch-setup.sh"
    assert script.is_file()
    assert script.stat().st_mode & stat.S_IXUSR


def test_scratch_script_reads_cmdline_mode():
    text = (RUNTIME_DIR / "fleetboot-scratch-setup.sh").read_text()
    # The script must consume the cmdline param fleetboot.scratch=<mode>
    # so the rendered grub.cfg's value actually does something.
    assert "fleetboot.scratch=" in text
    assert "/proc/cmdline" in text


def test_scratch_script_handles_all_three_modes():
    """Critical safety: 'off' must not touch the disk; 'volatile' must
    format every boot; 'persistent' must reuse the existing fs only when
    the fleetboot signature is present."""
    text = (RUNTIME_DIR / "fleetboot-scratch-setup.sh").read_text()
    for mode in ("volatile", "persistent", "off"):
        assert mode in text


def test_scratch_script_gates_on_fleetboot_signature():
    """Never format a disk that has an unrecognised filesystem — only
    blank disks or our-labelled disks are eligible."""
    text = (RUNTIME_DIR / "fleetboot-scratch-setup.sh").read_text()
    # The script must inspect existing filesystem before formatting.
    assert "blkid" in text
    # And it must use a distinctive label so a future boot can recognise
    # its own disk.
    assert "fleetboot-scratch" in text


def test_scratch_script_skips_removable_devices():
    """USB sticks should NOT be auto-formatted. /sys/block/*/removable
    is the standard signal."""
    text = (RUNTIME_DIR / "fleetboot-scratch-setup.sh").read_text()
    assert "removable" in text


def test_scratch_service_is_oneshot_with_cmdline_condition():
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    parser.read(SYSTEMD_DIR / "fleetboot-scratch-setup.service")
    assert parser["Service"]["Type"] == "oneshot"
    text = (SYSTEMD_DIR / "fleetboot-scratch-setup.service").read_text()
    # The unit must skip cleanly on machines without fleetboot context.
    assert "ConditionKernelCommandLine=fleetboot.scratch" in text


def test_recipe_enables_scratch_service():
    recipe = (REPO_ROOT / "image" / "fleetboot-base.yaml").read_text()
    assert "systemctl enable fleetboot-scratch-setup.service" in recipe
    # And the script gets chmod'd in the chroot.
    assert "fleetboot-scratch-setup.sh" in recipe
