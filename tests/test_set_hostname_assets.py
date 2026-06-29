"""Structural tests for the DHCP-from-initramfs hostname-adoption pieces."""

from __future__ import annotations

import configparser
import stat
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_DIR = REPO_ROOT / "image" / "runtime"
SYSTEMD_DIR = REPO_ROOT / "image" / "systemd"


def test_set_hostname_script_exists_and_executable():
    s = RUNTIME_DIR / "fleetboot-set-hostname.sh"
    assert s.is_file()
    assert s.stat().st_mode & stat.S_IXUSR


def test_set_hostname_reads_initramfs_state_files():
    """The script must read /run/net-*.conf, where initramfs ipconfig
    leaves the DHCP-supplied hostname. Anything else (dhclient hooks,
    NM dispatchers) depends on a later DHCP4 event that may never come
    because NM sees the interface as already configured."""
    text = (RUNTIME_DIR / "fleetboot-set-hostname.sh").read_text()
    assert "/run/net-" in text
    assert "HOSTNAME=" in text


def test_set_hostname_calls_hostnamectl():
    text = (RUNTIME_DIR / "fleetboot-set-hostname.sh").read_text()
    assert "hostnamectl" in text


def test_set_hostname_service_runs_before_reporter():
    """Ordering: hostname adopted BEFORE fleetboot-network-up.service
    so the reporter's socket.gethostname() returns the right value."""
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    parser.read(SYSTEMD_DIR / "fleetboot-set-hostname.service")
    assert parser["Service"]["Type"] == "oneshot"
    before = parser["Unit"].get("Before", "")
    assert "fleetboot-network-up.service" in before


def test_recipe_enables_set_hostname_service():
    recipe = (REPO_ROOT / "image" / "fleetboot-base.yaml").read_text()
    assert "systemctl enable fleetboot-set-hostname.service" in recipe
    assert "fleetboot-set-hostname.sh" in recipe
