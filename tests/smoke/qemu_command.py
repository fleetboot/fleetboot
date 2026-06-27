"""Builds the QEMU command line for the image smoke test.

Kept as a pure function so the unit tests under `make test` can assert the
wiring without spawning QEMU. The orchestrator subprocess-execs whatever this
returns.

The image is fetched over HTTP at boot via live-boot's `fetch=` mechanism,
not mounted from a disk. That mirrors the real netboot path: tftpjail will
serve kernel + initrd, and the squashfs lands over HTTP.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


# OVMF firmware paths on Debian. The CODE volume is read-only; we pass a
# writable copy of VARS so QEMU has somewhere to store EFI variables.
OVMF_CODE = Path("/usr/share/OVMF/OVMF_CODE_4M.fd")
OVMF_VARS = Path("/usr/share/OVMF/OVMF_VARS_4M.fd")


@dataclass(frozen=True)
class QemuRunSpec:
    """Inputs the orchestrator collects before constructing the command."""

    qemu_binary: str            # e.g. "qemu-system-x86_64"
    kernel: Path                # build/vmlinuz
    initrd: Path                # build/initrd.img
    fetch_url: str              # where the guest can GET the squashfs
    openschool_server_url: str  # base URL the reporter posts to
    boot_token: str             # per-boot session token
    host_port: int              # host TCP port to forward into the guest
    vars_file: Path             # writable copy of OVMF_VARS for this run
    memory_mb: int = 1024
    serial_log: Path | None = None


def build_kernel_cmdline(spec: QemuRunSpec) -> str:
    """Assemble the kernel command line.

    - `boot=live fetch=URL`: live-boot mode, fetch the squashfs over HTTP.
    - `ip=dhcp`: kernel-level DHCP so live-boot has network in the initrd.
    - `openschool.server=` / `openschool.boot_token=`: read by our reporter
      from /proc/cmdline; this is exactly the channel tftpjail will use in
      production to deliver the per-boot session token.
    - `console=ttyS0`: serial console for headless boot.
    """
    return " ".join(
        [
            "boot=live",
            f"fetch={spec.fetch_url}",
            "ip=dhcp",
            f"openschool.server={spec.openschool_server_url}",
            f"openschool.boot_token={spec.boot_token}",
            "console=ttyS0",
            "quiet",
        ]
    )


def build_qemu_command(spec: QemuRunSpec) -> list[str]:
    """Return the argv for spawning QEMU with our smoke-test setup.

    User-mode networking puts the guest at 10.0.2.15 and exposes the host at
    10.0.2.2 — that's where the guest will fetch the squashfs and POST status,
    so the orchestrator must pass `host_port` and a corresponding URL using
    10.0.2.2.
    """
    cmdline = build_kernel_cmdline(spec)
    argv: list[str] = [
        spec.qemu_binary,
        "-machine", "q35,accel=tcg",
        "-cpu", "max",
        "-m", str(spec.memory_mb),
        # UEFI firmware (split CODE/VARS form expected by modern OVMF).
        "-drive", f"if=pflash,format=raw,readonly=on,file={OVMF_CODE}",
        "-drive", f"if=pflash,format=raw,file={spec.vars_file}",
        # Direct kernel boot — OVMF accepts -kernel pass-through.
        "-kernel", str(spec.kernel),
        "-initrd", str(spec.initrd),
        "-append", cmdline,
        # User-mode networking. No hostfwd needed — the guest reaches the
        # host's listening port directly via 10.0.2.2.
        "-netdev", "user,id=net0",
        "-device", "virtio-net-pci,netdev=net0",
        # Headless: no graphics, serial on stdio so the orchestrator can log
        # the kernel/userspace boot for diagnostics.
        "-nographic",
        "-no-reboot",
    ]
    if spec.serial_log is not None:
        # Tee serial output to a file as well as the orchestrator's stdout.
        argv += ["-serial", f"file:{spec.serial_log}"]
    return argv
