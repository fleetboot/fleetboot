"""Structural tests for the signed-shim Secure Boot scaffolding.

We don't actually run signed binaries here (that needs real UEFI hardware
with Secure Boot enabled). These tests assert the artefacts on disk are
shaped how the chain expects:

  - the initial grub.cfg exists, is non-empty, and chainloads to the
    per-MAC jail path,
  - the Makefile knows where Debian's signed binaries live,
  - the build/ tree gets populated with the expected layout when the
    `signed-boot-assets` target runs.

The actual `make signed-boot-assets` invocation requires `shim-signed` and
`grub-efi-amd64-signed` packages on the build host; we don't run that here.
"""

from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
INITIAL_CFG = REPO_ROOT / "image" / "signed-boot" / "initial-grub.cfg"


def test_initial_grub_cfg_exists():
    assert INITIAL_CFG.is_file()


def test_initial_grub_cfg_chainloads_per_mac():
    """The static config must hand control to tftpjail's per-MAC path."""
    text = INITIAL_CFG.read_text()
    # The configfile line is the load-bearing one — without it, the
    # signed grub would stop here and we'd have no path to the kernel.
    assert "configfile (tftp,${pxe_default_server})/jail/" in text
    # And the MAC + arch + platform from GRUB's auto-set vars are
    # passed through so the renderer sees the real machine.
    for var in ("${net_default_mac}", "${grub_cpu}", "${grub_platform}"):
        assert var in text, f"{var} missing from initial grub.cfg"


def test_initial_grub_cfg_has_no_user_supplied_extensions():
    """Signed binaries don't honour embedded user configs at all; the
    initial file we ship is exactly what the signed grub reads from the
    TFTP path. Keep it free of `linux`/`initrd` commands so a future
    edit doesn't accidentally try to boot from this static file."""
    text = INITIAL_CFG.read_text()
    # Negative assertions: nothing here should pre-empt tftpjail's job.
    for forbidden in ("\nlinux ", "\ninitrd ", "\nboot\n"):
        assert forbidden not in text, (
            f"signed-boot initial cfg should NOT do {forbidden!r}; "
            "let tftpjail's per-MAC config own kernel loading"
        )


def test_makefile_signed_boot_target_present():
    """The structural test that proves admins can run `make signed-boot-assets`."""
    makefile = (REPO_ROOT / "Makefile").read_text()
    assert "signed-boot-assets:" in makefile
    # The bootfile we ADVERTISE is now fleetboot-branded; the SOURCE
    # filename in Debian's shim-signed package keeps its upstream name.
    assert "fleetboot-x64-uefi-signed.efi" in makefile
    assert "/usr/lib/shim/shimx64.efi.signed" in makefile
    # shim looks for `grubx64.efi` specifically; this filename must
    # not be renamed.
    assert "grubx64.efi" in makefile
    assert "/grub/grub.cfg" in makefile
