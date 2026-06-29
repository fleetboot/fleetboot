"""Render a per-MAC ``grub.cfg`` to be served by tftpjail.

This is the *content* tftpjail delivers when a client TFTP-fetches its
profile config. The boot session token has just been minted upstream
(``fleetboot_client``) — we stamp it into every URL the bootloader and
kernel will use, so:

  - GRUB fetches the kernel and initrd via authenticated HTTP,
  - live-boot fetches the squashfs via authenticated HTTP,
  - the in-image reporter reads the token from the kernel cmdline and uses
    it for ``POST /status``.

Pure function — no I/O — so it's exhaustively unit-testable.
"""

from __future__ import annotations

from typing import Final
from urllib.parse import urlparse

from tftpjail.identity import ClientIdentity


# Map our internal architecture names to the Debian package architecture
# strings that are baked into the squashfs filename in fleetboot. (Our
# image recipe produces ``fleetboot-amd64.squashfs`` / ``fleetboot-arm64.squashfs``.)
_ARCH_TO_DEBIAN: Final[dict[str, str]] = {
    "x86_64": "amd64",
    "arm64": "arm64",
    "i386": "i386",
}


def squashfs_filename_for(architecture: str, profile: str = "default") -> str:
    """Return the squashfs filename tftpjail should point the client at.

    The image build produces ``fleetboot-<profile>-<arch>.squashfs`` —
    the per-MAC ``profile_name`` is supplied by fleetboot's registry.
    """
    debian_arch = _ARCH_TO_DEBIAN.get(architecture)
    if debian_arch is None:
        raise ValueError(f"unsupported architecture: {architecture!r}")
    if not profile or not profile.replace("-", "").isalnum():
        raise ValueError(f"unsupported profile name: {profile!r}")
    return f"fleetboot-{profile}-{debian_arch}.squashfs"


def render_grub_cfg(
    *,
    identity: ClientIdentity,
    fleetboot_base_url: str,
    boot_token: str,
    profile: str = "default",
    serial_console: bool = False,
    target_architecture: str | None = None,
    scratch_mode: str = "volatile",
) -> str:
    """Render the per-MAC ``grub.cfg`` body.

    The base URL must include the scheme, host, and (if non-default) port;
    we parse it into GRUB's native ``(http,host:port)/path`` device syntax
    because GRUB's ``linux``/``initrd`` commands expect that form. A plain
    ``http://...`` URL passed to ``linux`` makes GRUB attempt a DNS lookup
    on the entire URL string and fail with ``no DNS record found``.

    ``serial_console`` controls whether the kernel cmdline includes
    ``console=ttyS0`` — wanted in VMs and headless debug hardware but a waste
    of cycles (and a small log-leak surface) on real desktops with no serial.

    ``target_architecture`` is the architecture the squashfs was BUILT for —
    distinct from ``identity.architecture``, which is the architecture of
    the GRUB binary that's asking. A BIOS-PXE'd x86_64 machine runs a
    32-bit GRUB (``identity.architecture == 'i386'``) but boots a 64-bit
    kernel and amd64 squashfs (``target_architecture == 'x86_64'``).
    Defaults to ``identity.architecture`` for backwards compatibility.
    """
    parsed = urlparse(fleetboot_base_url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(
            f"fleetboot_base_url must be http(s); got {fleetboot_base_url!r}"
        )
    # GRUB fetches kernel + initrd via TFTP, not HTTP: its HTTP module
    # allocates Content-Length up front and dies with "out of memory" on a
    # 40 MB initrd in an EFI guest. TFTP streams block-by-block. Both files
    # are served by tftpjail from its public-assets directory (the same
    # mechanism that serves grubnetx64.efi). The TFTP server is whichever
    # host DHCP told the firmware via next-server, exposed by GRUB as
    # $pxe_default_server.
    grub_dev = "(tftp,${pxe_default_server})"
    arch_for_squashfs = target_architecture or identity.architecture
    squashfs = squashfs_filename_for(arch_for_squashfs, profile)

    kernel_url = f"{grub_dev}/vmlinuz"
    initrd_url = f"{grub_dev}/initrd.img"

    # The kernel's live-boot needs a normal HTTP URL it can curl-fetch. The
    # token goes in a PATH SEGMENT, not the query string: live-boot derives
    # the archive type by taking everything after the last `.` in the URL,
    # so a `?t=…` tail would turn `.squashfs` into `squashfs?t=…` and the
    # file becomes "Unrecognised archive extension". Path segment form
    # keeps `.squashfs` as the actual extension.
    host_port = parsed.netloc
    base_path = parsed.path.rstrip("/")
    cmdline_squashfs = (
        f"{parsed.scheme}://{host_port}{base_path}/boot/{boot_token}/{squashfs}"
    )
    cmdline_server = f"{parsed.scheme}://{host_port}{base_path}/"

    cmdline_parts = [
        "boot=live",
        f"fetch={cmdline_squashfs}",
        "ip=dhcp",
        f"fleetboot.server={cmdline_server}",
        f"fleetboot.boot_token={boot_token}",
        # The image's fleetboot-scratch-setup.service reads this off the
        # kernel cmdline and decides what to do with the local disk. The
        # value is validated server-side; we just stamp it through.
        f"fleetboot.scratch={scratch_mode}",
    ]
    if serial_console:
        # Print kernel output to BOTH local tty0 AND serial. Listing more
        # than one `console=` means the kernel copies its log to each;
        # the LAST one becomes /dev/console for init scripts. Without
        # `console=tty0` the local monitor goes blank during kernel
        # boot — the screen only shows whatever userspace puts on it
        # afterwards. With both, you see the boot on either the serial
        # cable OR the attached monitor.
        cmdline_parts.append("console=tty0")
        cmdline_parts.append("console=ttyS0,115200n8")
    else:
        # Real-hardware default: quiet so the user sees the splash, not
        # a wall of kernel printks. No serial port advertised.
        cmdline_parts.append("quiet")
    kernel_cmdline = " ".join(cmdline_parts)

    # When serial_console is on, re-emit GRUB's serial setup here so the
    # per-MAC config block also prints to serial. The embedded.cfg in our
    # GRUB binaries enables serial unconditionally, but some firmware
    # stacks reset the terminal between configfile invocations — this
    # belts-and-braces it so the entire boot loader stage is visible on
    # the serial port for serial-attached debug hosts.
    serial_prelude = ""
    if serial_console:
        serial_prelude = (
            "serial --unit=0 --speed=115200\n"
            "terminal_input --append serial\n"
            "terminal_output --append serial\n"
        )

    # GRUB-emitted boot-stage events go over TFTP, not HTTP. BIOS GRUB's
    # HTTP stack on the OptiPlex's UNDI driver added ~34s per call
    # (likely TCP FIN-propagation timing on the old NIC); UDP TFTP
    # through the same UNDI path serves kernels in milliseconds.
    # tftpjail's rrq_intercept callback (registered by fleetboot)
    # recognises this URL shape and records the event in-process,
    # returning empty bytes so `source` parses a no-op script.
    #
    # The HTTP /grub-event endpoint stays in fleetboot as a fallback
    # for clients with a less-broken TCP stack (UEFI, modern x86), but
    # the renderer's default is TFTP. ${pxe_default_server} is a GRUB
    # variable expanded at runtime to the TFTP server's IP.
    def _event(state: str) -> str:
        return (
            f"source (tftp,${{pxe_default_server}})/grub-event/"
            f"{boot_token}/{state}\n"
        )

    # Echo each step so a user with a monitor sees what GRUB is doing.
    # `set debug=` is too noisy; explicit echoes are the right verbosity
    # for boot diagnosis.
    return (
        "set timeout=0\n"
        + serial_prelude
        + f"echo \"fleetboot: booting {identity.asserted_mac} {identity.architecture}/{identity.platform}\"\n"
        + "echo \"fleetboot: > grub_running event\"\n"
        + _event("grub_running")
        + "echo \"fleetboot: > linux kernel\"\n"
        + f"linux {kernel_url} {kernel_cmdline}\n"
        + "echo \"fleetboot: > kernel_loaded event\"\n"
        + _event("kernel_loaded")
        + "echo \"fleetboot: > initrd\"\n"
        + f"initrd {initrd_url}\n"
        + "echo \"fleetboot: > initrd_loaded event\"\n"
        + _event("initrd_loaded")
        + "echo \"fleetboot: > booting_kernel event\"\n"
        + _event("booting_kernel")
        + "echo \"fleetboot: handing off to kernel\"\n"
        + "boot\n"
    )
