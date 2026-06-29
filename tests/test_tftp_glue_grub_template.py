"""Tests for the per-MAC grub.cfg renderer."""

import pytest

from fleetboot.tftp_glue.grub_template import render_grub_cfg, squashfs_filename_for
from tftpjail.identity import ClientIdentity


@pytest.fixture
def identity() -> ClientIdentity:
    return ClientIdentity(
        asserted_mac="aa:bb:cc:dd:ee:ff",
        architecture="x86_64",
        platform="efi",
        system_uuid="12345678-1234-1234-1234-123456789abc",
    )


def test_squashfs_filename_for_known_arches_with_default_profile():
    assert squashfs_filename_for("x86_64") == "fleetboot-default-amd64.squashfs"
    assert squashfs_filename_for("arm64") == "fleetboot-default-arm64.squashfs"
    assert squashfs_filename_for("i386") == "fleetboot-default-i386.squashfs"


def test_squashfs_filename_includes_profile():
    assert (
        squashfs_filename_for("x86_64", profile="school")
        == "fleetboot-school-amd64.squashfs"
    )


def test_squashfs_filename_rejects_unknown_arch():
    with pytest.raises(ValueError):
        squashfs_filename_for("mips64")


def test_rendered_cfg_emits_grub_stage_events(identity: ClientIdentity):
    """The renderer sprinkles `source (tftp,...)/grub-event/...` lines
    between linux/initrd/boot. tftpjail's rrq_intercept callback
    recognises these and forwards to fleetboot in-process — TFTP over
    UDP avoids the ~34s per-call TCP FIN-propagation delay the
    OptiPlex's BIOS PXE stack has on HTTP."""
    cfg = render_grub_cfg(
        identity=identity,
        fleetboot_base_url="http://10.0.2.2:8080/",
        boot_token="deadbeef",
    )
    for state in (
        "grub_running",
        "kernel_loaded",
        "initrd_loaded",
        "booting_kernel",
    ):
        assert f"/grub-event/deadbeef/{state}" in cfg
        # Each event must use the TFTP transport (UDP), not HTTP.
        # ${pxe_default_server} expands at GRUB runtime.
        assert (
            f"source (tftp,${{pxe_default_server}})/grub-event/deadbeef/{state}"
            in cfg
        )
    # The events must be ordered through the boot stages.
    g = cfg.index("grub_running")
    k = cfg.index("kernel_loaded")
    i = cfg.index("initrd_loaded")
    b = cfg.index("booting_kernel")
    assert g < k < i < b


def test_rendered_cfg_with_serial_emits_grub_serial_setup(
    identity: ClientIdentity,
):
    """With serial_console=true, the per-MAC config must also enable
    GRUB serial so the boot loader stage is visible on the serial port,
    not just the kernel."""
    cfg = render_grub_cfg(
        identity=identity,
        fleetboot_base_url="http://10.0.2.2:8080/",
        boot_token="deadbeef",
        serial_console=True,
    )
    assert "serial --unit=0 --speed=115200" in cfg
    assert "terminal_input --append serial" in cfg
    assert "terminal_output --append serial" in cfg


def test_rendered_cfg_without_serial_omits_grub_serial(
    identity: ClientIdentity,
):
    """No serial_console flag — no per-MAC serial setup. Embedded.cfg
    still does its own thing; we just don't duplicate."""
    cfg = render_grub_cfg(
        identity=identity,
        fleetboot_base_url="http://10.0.2.2:8080/",
        boot_token="deadbeef",
        serial_console=False,
    )
    # The kernel cmdline shouldn't have console=ttyS0 either.
    assert "serial --unit=0" not in cfg
    assert "console=ttyS0" not in cfg


def test_rendered_cfg_stamps_scratch_mode_into_cmdline(
    identity: ClientIdentity,
):
    """The image's scratch-setup reads fleetboot.scratch=<mode> from the
    cmdline, so the renderer must put it there in every mode."""
    for mode in ("volatile", "persistent", "off"):
        cfg = render_grub_cfg(
            identity=identity,
            fleetboot_base_url="http://10.0.2.2:8080/",
            boot_token="deadbeef",
            scratch_mode=mode,
        )
        assert f"fleetboot.scratch={mode}" in cfg


def test_rendered_cfg_defaults_to_volatile_scratch(identity: ClientIdentity):
    cfg = render_grub_cfg(
        identity=identity,
        fleetboot_base_url="http://10.0.2.2:8080/",
        boot_token="deadbeef",
    )
    assert "fleetboot.scratch=volatile" in cfg


def test_rendered_cfg_target_architecture_wins_over_identity():
    """A BIOS-PXE'd x86_64 machine runs a 32-bit GRUB (identity says
    `i386`) but must boot the amd64 squashfs. target_architecture is
    the override."""
    bios_grub_identity = ClientIdentity(
        asserted_mac="aa:bb:cc:dd:ee:ff",
        architecture="i386",  # what the BIOS GRUB binary reports
        platform="pc",
        system_uuid="12345678-1234-1234-1234-123456789abc",
    )
    cfg = render_grub_cfg(
        identity=bios_grub_identity,
        fleetboot_base_url="http://10.0.2.2:8080/",
        boot_token="deadbeef",
        target_architecture="x86_64",  # what the machine actually IS
    )
    # The squashfs URL points at the amd64 image, not the i386 one.
    assert "/fleetboot-default-amd64.squashfs" in cfg
    assert "/fleetboot-default-i386.squashfs" not in cfg


def test_rendered_cfg_falls_back_to_identity_architecture(
    identity: ClientIdentity,
):
    """Without target_architecture, the renderer keeps the legacy
    behaviour: use identity.architecture for the squashfs lookup."""
    cfg = render_grub_cfg(
        identity=identity,  # architecture='x86_64'
        fleetboot_base_url="http://10.0.2.2:8080/",
        boot_token="deadbeef",
    )
    assert "/fleetboot-default-amd64.squashfs" in cfg


def test_rendered_cfg_stamps_token_into_squashfs_url_and_cmdline(
    identity: ClientIdentity,
):
    cfg = render_grub_cfg(
        identity=identity,
        fleetboot_base_url="http://10.0.2.2:8080/",
        boot_token="deadbeef",
    )
    # The squashfs URL carries the token as a path segment (NOT a query
    # string) so live-boot's URL parser still sees `.squashfs` as the
    # extension. The kernel cmdline embeds the token separately for the
    # in-image reporter.
    assert "/boot/deadbeef/fleetboot-default-amd64.squashfs" in cfg
    assert "fleetboot.boot_token=deadbeef" in cfg
    # Kernel and initrd are served by tftpjail's public-assets path over
    # TFTP, with no token in the URL (they have no secrets, identical for
    # everyone of a given arch).
    assert "vmlinuz?" not in cfg
    assert "initrd.img?" not in cfg


def test_rendered_cfg_uses_arch_specific_squashfs(identity: ClientIdentity):
    cfg = render_grub_cfg(
        identity=identity,
        fleetboot_base_url="http://server/",
        boot_token="tok",
    )
    assert "fleetboot-default-amd64.squashfs" in cfg


def test_rendered_cfg_for_arm64_picks_arm64_squashfs():
    arm_identity = ClientIdentity(
        asserted_mac="aa:bb:cc:dd:ee:ff",
        architecture="arm64",
        platform="efi",
        system_uuid=None,
    )
    cfg = render_grub_cfg(
        identity=arm_identity,
        fleetboot_base_url="http://server/",
        boot_token="tok",
    )
    assert "fleetboot-default-arm64.squashfs" in cfg
    assert "fleetboot-default-amd64.squashfs" not in cfg


def test_rendered_cfg_sets_fleetboot_server_url(identity: ClientIdentity):
    cfg = render_grub_cfg(
        identity=identity,
        fleetboot_base_url="http://10.0.2.2:8080",
        boot_token="tok",
    )
    # The reporter inside the image parses fleetboot.server= from cmdline.
    assert "fleetboot.server=http://10.0.2.2:8080/" in cfg


def test_rendered_cfg_has_linux_initrd_boot_directives(identity: ClientIdentity):
    cfg = render_grub_cfg(
        identity=identity,
        fleetboot_base_url="http://server/",
        boot_token="tok",
    )
    assert "linux " in cfg
    assert "initrd " in cfg
    assert "\nboot\n" in cfg


def test_rendered_cfg_strips_trailing_slash_on_base_url(identity: ClientIdentity):
    """Both forms of base URL must produce identical bytes."""
    a = render_grub_cfg(
        identity=identity,
        fleetboot_base_url="http://server/",
        boot_token="tok",
    )
    b = render_grub_cfg(
        identity=identity,
        fleetboot_base_url="http://server",
        boot_token="tok",
    )
    assert a == b


def test_rendered_cfg_records_the_mac_in_a_comment(identity: ClientIdentity):
    cfg = render_grub_cfg(
        identity=identity,
        fleetboot_base_url="http://server/",
        boot_token="tok",
    )
    assert identity.asserted_mac in cfg


def test_serial_console_omitted_by_default(identity: ClientIdentity):
    """Real desktops have no serial; cmdline must not mention ttyS0."""
    cfg = render_grub_cfg(
        identity=identity,
        fleetboot_base_url="http://server/",
        boot_token="tok",
    )
    assert "console=ttyS0" not in cfg


def test_serial_console_added_when_flag_set(identity: ClientIdentity):
    """VMs / headless hardware get console=ttyS0 only when explicitly opted in.

    Both consoles are kept active: ttyS0 (for the serial cable) AND tty0
    (the local monitor). Without `console=tty0`, the local screen goes
    blank during kernel boot — kernel sends everything to the LAST
    `console=` only, and we want operators to see boot messages
    regardless of where they're looking.
    """
    cfg = render_grub_cfg(
        identity=identity,
        fleetboot_base_url="http://server/",
        boot_token="tok",
        serial_console=True,
    )
    assert "console=ttyS0,115200n8" in cfg
    assert "console=tty0" in cfg
