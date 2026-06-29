"""Structural tests for image/base-overlay/.

This overlay ships /etc fragments that every profile in the fleet
gets, regardless of inheritance. Currently:
  - kernel.sysrq=0 sysctl drop-in to stop floating ttyS0 noise from
    triggering silent reboots via Alt+SysRq+B.
"""

from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
BASE_OVERLAY = REPO_ROOT / "image" / "base-overlay"


def test_sysrq_is_disabled_via_sysctl_drop_in():
    drop_in = BASE_OVERLAY / "etc/sysctl.d/50-fleetboot-disable-sysrq.conf"
    assert drop_in.is_file()
    text = drop_in.read_text()
    assert "kernel.sysrq = 0" in text or "kernel.sysrq=0" in text


def test_recipe_overlays_base_overlay_at_root():
    """The recipe must include an overlay action sourcing base-overlay/
    onto /. Without it the sysctl drop-in never reaches the image."""
    recipe = (REPO_ROOT / "image" / "fleetboot-base.yaml").read_text()
    assert "./base-overlay" in recipe
    assert "destination: /" in recipe
