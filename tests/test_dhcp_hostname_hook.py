"""Structural tests for the DHCP option 12 hostname-honour pieces."""

from __future__ import annotations

import stat
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
HOOK = REPO_ROOT / "image" / "networking" / "dhclient" / "95-fleetboot-hostname"
NM_HOOK = REPO_ROOT / "image" / "networking" / "nm-dispatcher" / "95-fleetboot-hostname"


def test_hook_exists_and_is_executable():
    assert HOOK.is_file()
    assert HOOK.stat().st_mode & stat.S_IXUSR


def test_hook_acts_only_on_lease_grant_events():
    """A dhclient hook fires for every event including EXPIRE / RELEASE;
    setting the hostname on a release would clear it. Guard with the
    standard $reason whitelist."""
    text = HOOK.read_text()
    assert "BOUND" in text
    assert "RENEW" in text
    # No bare hostnamectl call outside the case branch.
    assert "case " in text


def test_hook_consumes_dhclient_new_host_name():
    text = HOOK.read_text()
    assert "new_host_name" in text
    # Must set the hostname, not just print it.
    assert "hostnamectl" in text or "hostname " in text


def test_recipe_overlays_hook_and_makes_it_executable():
    recipe = (REPO_ROOT / "image" / "fleetboot-base.yaml").read_text()
    assert "/etc/dhcp/dhclient-exit-hooks.d" in recipe
    assert "95-fleetboot-hostname" in recipe


def test_nm_dispatcher_hook_exists_and_consumes_dhcp4_host_name():
    """Sister hook for NetworkManager-managed networks. NM's default
    internal DHCP client never fires dhclient-exit-hooks, so without
    this script the hostname feature wouldn't work on any image that
    pulls in NetworkManager (which all the desktop profiles do)."""
    assert NM_HOOK.is_file()
    assert NM_HOOK.stat().st_mode & stat.S_IXUSR
    text = NM_HOOK.read_text()
    # NM exports DHCP options as DHCP4_* env vars.
    assert "DHCP4_HOST_NAME" in text
    # Only on lease grants, not on every dispatcher event.
    assert "up" in text and "dhcp4-change" in text


def test_recipe_also_installs_nm_dispatcher_hook():
    recipe = (REPO_ROOT / "image" / "fleetboot-base.yaml").read_text()
    assert "/etc/NetworkManager/dispatcher.d" in recipe
