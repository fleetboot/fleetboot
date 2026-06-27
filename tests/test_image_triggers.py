"""Tests that the in-image trigger assets (systemd units, PAM hook) are wired
to the boot states they claim to report and have the ordering directives we
rely on.

These do not run systemd — they parse the unit files. The integration test
that actually boots a UEFI guest will live with the image build step.
"""

import configparser
import os
import stat
from pathlib import Path

import pytest

from openschool.boot_states import BootState


REPO_ROOT = Path(__file__).resolve().parents[1]
SYSTEMD_DIR = REPO_ROOT / "image" / "systemd"
PAM_HOOK = REPO_ROOT / "image" / "pam" / "openschool-session-hook"


def _load_unit(name: str) -> configparser.ConfigParser:
    """Parse a systemd unit file. systemd's grammar is INI-compatible enough
    for our needs here."""
    parser = configparser.ConfigParser(
        # systemd allows duplicate keys (After=, Wants=). We don't have any in
        # the same unit *yet*, but be tolerant.
        strict=False,
        interpolation=None,
    )
    parser.read(SYSTEMD_DIR / name)
    return parser


@pytest.mark.parametrize(
    "unit_name, expected_state",
    [
        ("openschool-network-up.service", BootState.NETWORK_UP),
        ("openschool-nfs-mounted.service", BootState.NFS_MOUNTED),
        ("openschool-login-ready.service", BootState.LOGIN_READY),
    ],
)
def test_unit_reports_expected_state(unit_name: str, expected_state: BootState):
    unit = _load_unit(unit_name)
    exec_start = unit["Service"]["ExecStart"]
    assert "openschool.reporter.report" in exec_start
    assert expected_state.value in exec_start


def test_network_up_unit_waits_for_network_online():
    unit = _load_unit("openschool-network-up.service")
    assert "network-online.target" in unit["Unit"]["After"]
    assert "network-online.target" in unit["Unit"]["Wants"]


def test_nfs_unit_waits_for_home_mount_and_for_network_report():
    unit = _load_unit("openschool-nfs-mounted.service")
    after = unit["Unit"]["After"]
    assert "home.mount" in after
    assert "openschool-network-up.service" in after
    assert "home.mount" in unit["Unit"]["Requires"]


def test_login_ready_unit_waits_for_display_manager_and_nfs_report():
    unit = _load_unit("openschool-login-ready.service")
    after = unit["Unit"]["After"]
    assert "display-manager.service" in after
    assert "openschool-nfs-mounted.service" in after


@pytest.mark.parametrize(
    "unit_name",
    [
        "openschool-network-up.service",
        "openschool-nfs-mounted.service",
        "openschool-login-ready.service",
    ],
)
def test_units_are_oneshot(unit_name: str):
    unit = _load_unit(unit_name)
    assert unit["Service"]["Type"] == "oneshot"


def test_pam_hook_is_executable():
    mode = PAM_HOOK.stat().st_mode
    assert mode & stat.S_IXUSR, "PAM hook script must be executable"


def test_pam_hook_reports_user_logged_in_with_username():
    contents = PAM_HOOK.read_text()
    assert "user_logged_in" in contents
    assert "$PAM_USER" in contents


def test_pam_hook_only_fires_on_session_open():
    """Without this guard PAM would fire it on close too, double-counting."""
    contents = PAM_HOOK.read_text()
    assert "open_session" in contents


def test_pam_hook_is_failsafe_for_login():
    """A reporting failure must never block the user from logging in.

    We enforce that by requiring a `|| true` (or equivalent) on the report
    call line, so the script always exits 0 from PAM's perspective.
    """
    contents = PAM_HOOK.read_text()
    assert "|| true" in contents
