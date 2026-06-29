"""Tests that the in-image trigger assets (systemd units) are wired to
the boot states they claim to report and have the ordering directives
we rely on.

These do not run systemd — they parse the unit files. The integration
test that actually boots a UEFI guest will live with the image build
step.
"""

import configparser
from pathlib import Path

import pytest

from fleetboot.boot_states import BootState


REPO_ROOT = Path(__file__).resolve().parents[1]
SYSTEMD_DIR = REPO_ROOT / "image" / "systemd"


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
        ("fleetboot-network-up.service", BootState.NETWORK_UP),
        ("fleetboot-nfs-mounted.service", BootState.NFS_MOUNTED),
        ("fleetboot-login-console.service", BootState.LOGIN_CONSOLE),
    ],
)
def test_unit_reports_expected_state(unit_name: str, expected_state: BootState):
    unit = _load_unit(unit_name)
    exec_start = unit["Service"]["ExecStart"]
    assert "fleetboot.reporter.report" in exec_start
    assert expected_state.value in exec_start


def test_network_up_unit_waits_for_network_online():
    unit = _load_unit("fleetboot-network-up.service")
    assert "network-online.target" in unit["Unit"]["After"]
    assert "network-online.target" in unit["Unit"]["Wants"]


def test_nfs_unit_waits_for_home_mount_and_for_network_report():
    unit = _load_unit("fleetboot-nfs-mounted.service")
    after = unit["Unit"]["After"]
    assert "home.mount" in after
    assert "fleetboot-network-up.service" in after
    assert "home.mount" in unit["Unit"]["Requires"]


def test_login_console_unit_waits_for_display_manager_and_nfs_report():
    unit = _load_unit("fleetboot-login-console.service")
    after = unit["Unit"]["After"]
    assert "display-manager.service" in after
    assert "fleetboot-nfs-mounted.service" in after


@pytest.mark.parametrize(
    "unit_name",
    [
        "fleetboot-network-up.service",
        "fleetboot-nfs-mounted.service",
        "fleetboot-login-console.service",
    ],
)
def test_units_are_oneshot(unit_name: str):
    unit = _load_unit(unit_name)
    assert unit["Service"]["Type"] == "oneshot"
