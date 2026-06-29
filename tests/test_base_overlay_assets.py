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


def test_initramfs_compression_is_zstd():
    """update-initramfs defaults to gzip; we want zstd for faster
    image-build time, faster in-image rebuilds (kernel upgrades,
    dkms), and faster cold-boot decompression on slow hardware."""
    drop_in = BASE_OVERLAY / "etc/initramfs-tools/conf.d/zstd-compression"
    assert drop_in.is_file()
    text = drop_in.read_text()
    assert "COMPRESS=zstd" in text


def test_base_recipe_installs_zstd():
    """COMPRESS=zstd in initramfs.conf only works if the `zstd` binary
    is actually present in the image."""
    recipe = (REPO_ROOT / "image" / "fleetboot-base.yaml").read_text()
    assert "\n      - zstd\n" in recipe


def test_set_hostname_script_falls_back_to_mac_suffix():
    """When DHCP doesn't hand out a HOSTNAME, the script should
    derive a stable placeholder from the primary interface's MAC
    address (last 6 hex chars). Without this, every freshly-PXE'd
    machine on a network without DHCP option 12 would identify as
    the baked-in default — unhelpful in the dashboard."""
    script = (
        REPO_ROOT / "image" / "runtime" / "fleetboot-set-hostname.sh"
    ).read_text()
    assert "/sys/class/net" in script
    assert 'fleetboot-' in script
    # The last-6-chars derivation uses tail -c 6 on the colon-stripped
    # MAC.
    assert "tail -c 6" in script


def test_recipe_overlays_base_overlay_at_root():
    """The recipe must include an overlay action sourcing base-overlay/
    onto /. Without it the sysctl drop-in never reaches the image."""
    recipe = (REPO_ROOT / "image" / "fleetboot-base.yaml").read_text()
    assert "./base-overlay" in recipe
    assert "destination: /" in recipe
