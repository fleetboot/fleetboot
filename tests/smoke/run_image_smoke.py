"""Orchestrator for the image smoke test.

Run via `make image-smoke`. Not part of `make test` because it spawns QEMU
and takes minutes.

Steps:
  1. Locate the build artifacts (kernel, initrd, squashfs).
  2. Start a stub OpenSchool server that ALSO serves the squashfs as a
     static file.
  3. Spawn QEMU UEFI direct-kernel boot, cmdline carrying a fresh per-boot
     token and the URL of the stub server.
  4. Wait for the reporter inside the booted image to POST `network_up`.
  5. Print success or failure and shut everything down.
"""

from __future__ import annotations

import argparse
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

from tests.smoke.qemu_command import (
    OVMF_CODE,
    OVMF_VARS,
    QemuRunSpec,
    build_qemu_command,
)
from tests.smoke.stub_server import find_free_port, running_stub_server


# IP user-mode networking exposes the host at this address inside the guest.
GUEST_VIEW_OF_HOST = "10.0.2.2"

# Generous timeout: a UEFI cold boot of a fresh Debian under TCG plus a
# squashfs HTTP fetch can take several minutes.
DEFAULT_BOOT_TIMEOUT_SECONDS = 600


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-dir", required=True, type=Path)
    parser.add_argument("--arch", default="amd64")
    parser.add_argument(
        "--mac",
        default="52:54:00:12:34:56",
        help="MAC address the stub session is minted for",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_BOOT_TIMEOUT_SECONDS,
        help="seconds to wait for network_up before failing",
    )
    return parser.parse_args(argv)


def locate_artifacts(build_dir: Path, arch: str) -> tuple[Path, Path, Path]:
    """Resolve and validate the three required artifacts."""
    kernel = build_dir / "vmlinuz"
    initrd = build_dir / "initrd.img"
    squashfs = build_dir / f"openschool-{arch}.squashfs"
    missing = [p for p in (kernel, initrd, squashfs) if not p.is_file()]
    if missing:
        raise SystemExit(
            "missing image artifacts: "
            + ", ".join(str(p) for p in missing)
            + "\n\nRun `make image` first."
        )
    return kernel, initrd, squashfs


def check_ovmf_present() -> None:
    missing = [p for p in (OVMF_CODE, OVMF_VARS) if not p.is_file()]
    if missing:
        raise SystemExit(
            "OVMF firmware not found: " + ", ".join(str(p) for p in missing)
            + "\n\nInstall the `ovmf` Debian package."
        )


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    kernel, initrd, squashfs = locate_artifacts(args.build_dir, args.arch)
    check_ovmf_present()

    qemu_binary = shutil.which("qemu-system-x86_64")
    if qemu_binary is None:
        raise SystemExit("qemu-system-x86_64 not found in PATH")

    host_port = find_free_port()
    serial_log = args.build_dir / "smoke-serial.log"

    with tempfile.TemporaryDirectory(prefix="openschool-smoke-") as scratch:
        scratch_path = Path(scratch)
        # OVMF wants a writable VARS copy per-run; the shipped file is shared.
        vars_file = scratch_path / "OVMF_VARS.fd"
        shutil.copyfile(OVMF_VARS, vars_file)

        with running_stub_server(
            host="127.0.0.1",
            port=host_port,
            mac=args.mac,
            squashfs_path=squashfs,
        ) as stub:
            spec = QemuRunSpec(
                qemu_binary=qemu_binary,
                kernel=kernel,
                initrd=initrd,
                fetch_url=(
                    f"http://{GUEST_VIEW_OF_HOST}:{host_port}/openschool.squashfs"
                ),
                openschool_server_url=(
                    f"http://{GUEST_VIEW_OF_HOST}:{host_port}/"
                ),
                boot_token=stub.boot_token,
                host_port=host_port,
                vars_file=vars_file,
                serial_log=serial_log,
            )
            cmd = build_qemu_command(spec)
            print(f"smoke: launching {' '.join(cmd)}", flush=True)
            qemu = subprocess.Popen(cmd, stdout=sys.stdout, stderr=sys.stderr)
            try:
                if stub.wait_for_network_up(timeout=args.timeout):
                    print("smoke: SUCCESS — network_up received", flush=True)
                    return 0
                print(
                    f"smoke: FAILED — no network_up in {args.timeout}s "
                    f"(serial log at {serial_log})",
                    file=sys.stderr,
                )
                return 1
            finally:
                # Best-effort shutdown of the guest.
                qemu.send_signal(signal.SIGTERM)
                try:
                    qemu.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    qemu.kill()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
