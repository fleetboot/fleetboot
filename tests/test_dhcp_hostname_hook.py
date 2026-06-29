"""Structural tests for the DHCP option 12 hostname-honour pieces."""

from __future__ import annotations

import stat
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
HOOK = REPO_ROOT / "image" / "networking" / "95-fleetboot-hostname"


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
