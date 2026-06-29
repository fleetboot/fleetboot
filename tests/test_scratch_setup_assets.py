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


def _action_case_body(text: str) -> str:
    """Return the second case statement — the one with mkfs.ext4 calls."""
    # The first case validates the mode value; the second does the work.
    # Find the case that contains 'mkfs.ext4'.
    pieces = text.split("case \"$mode\" in")
    for piece in pieces[1:]:
        if "mkfs.ext4" in piece.split("esac", 1)[0]:
            return piece.split("esac", 1)[0]
    raise AssertionError("no action `case` containing mkfs.ext4 found")


def test_persistent_mode_refuses_to_wipe_unknown_fs():
    """Persistent mode never formats a disk that has an unrecognised
    filesystem — protects user data when admin meant to preserve."""
    body = _action_case_body(
        (RUNTIME_DIR / "fleetboot-scratch-setup.sh").read_text(),
    )
    persistent_idx = body.find("persistent)")
    assert persistent_idx > 0
    persistent_section = body[persistent_idx:body.find(";;", persistent_idx)]
    assert "refusing to wipe" in persistent_section


def test_volatile_mode_wipes_unconditionally():
    """Choosing volatile means "this disk is scratch, anything on it is
    replaceable". Persistent state lives on NFS /home; the local disk
    is just RAM-shaped."""
    body = _action_case_body(
        (RUNTIME_DIR / "fleetboot-scratch-setup.sh").read_text(),
    )
    volatile_idx = body.find("volatile)")
    assert volatile_idx > 0
    volatile_section = body[volatile_idx:body.find(";;", volatile_idx)]
    assert "refusing to wipe" not in volatile_section
    assert "mkfs.ext4" in volatile_section


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


def test_scratch_service_reports_scratch_mounted_on_success():
    """ExecStartPost calls the reporter so the dashboard sees the
    scratch_mounted state — visibility into 'did the mount actually
    happen'."""
    text = (SYSTEMD_DIR / "fleetboot-scratch-setup.service").read_text()
    assert "fleetboot.reporter.report scratch_mounted" in text
    # Leading `-` means a failed reporter call doesn't mark the unit
    # failed — the next heartbeat will re-send the state.
    assert "ExecStartPost=-/" in text


def test_scratch_service_waits_for_network():
    """The reporter call needs HTTP; defer the whole unit until
    network-online so the report can actually get through."""
    text = (SYSTEMD_DIR / "fleetboot-scratch-setup.service").read_text()
    assert "network-online.target" in text


def test_scratch_script_fails_loud_when_mkfs_missing():
    """Without mkfs.ext4 the script can't do its job — exit non-zero so
    the unit is marked failed and the dashboard's diagnostics surface
    'scratch broken' instead of pretending success."""
    text = (RUNTIME_DIR / "fleetboot-scratch-setup.sh").read_text()
    assert "command -v mkfs.ext4" in text
    # The check must be paired with a hard exit, not just a warning.
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if "command -v mkfs.ext4" in line:
            # Look for `exit 1` in the next ~6 lines.
            window = "\n".join(lines[i:i + 8])
            assert "exit 1" in window, "mkfs missing check must exit non-zero"
            break


def test_base_recipe_installs_e2fsprogs():
    """mkfs.ext4 lives in e2fsprogs; the scratch setup needs it."""
    recipe = (REPO_ROOT / "image" / "fleetboot-base.yaml").read_text()
    assert "\n      - e2fsprogs\n" in recipe


def test_recipe_enables_scratch_service():
    recipe = (REPO_ROOT / "image" / "fleetboot-base.yaml").read_text()
    assert "systemctl enable fleetboot-scratch-setup.service" in recipe
    # And the script gets chmod'd in the chroot.
    assert "fleetboot-scratch-setup.sh" in recipe
