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


def test_set_hostname_writes_etc_hostname_and_calls_hostname():
    """The script must persist the hostname by writing /etc/hostname AND
    set the live kernel hostname. hostnamectl was unreliable in early
    boot — systemd-hostnamed is socket-activated and not always ready
    when this script runs, so a hostnamectl failure was silent and
    NetworkManager later read /etc/hostname (still the baked default)
    and pushed it back over our transient set."""
    text = (RUNTIME_DIR / "fleetboot-set-hostname.sh").read_text()
    assert "/etc/hostname" in text
    assert "hostname " in text  # the legacy syscall-setting binary
    # And explicitly NOT relying on hostnamectl:
    assert "hostnamectl" not in text


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
