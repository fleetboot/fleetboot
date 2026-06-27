"""Does VirtualBox's UEFI guest actually PXE-fetch from an external TFTP server?

This is an *exploratory* smoke script, not a pytest test. We do not want to
run VirtualBox during every `make test` — this is a manual decision-point
check, run once to confirm whether the VBox path is viable for the CI
functional test we want to build.

What it does:

  1. Spin up a throwaway UEFI VirtualBox VM with NAT networking.
  2. Override the NAT DHCP fields so the guest sees:
        next-server = 10.0.2.2  (the host, from inside VBox NAT)
        bootfile    = <our marker name>
  3. Listen on UDP/69 on the host.
  4. Boot the VM headless.
  5. Wait up to N seconds for a TFTP RRQ to land.
  6. Print what we saw (source IP, requested filename, the raw packet) and
     tear the VM down.

Run as:

    sudo python3 -m tests.vbox_uefi_pxe_smoke

or, if you've granted Python `cap_net_bind_service`, without sudo. Pass
`--port 6969` to listen on an unprivileged port — useful for debugging,
but UEFI PXE talks to UDP/69 on the wire so a "did the guest reach us"
verdict needs the real port.
"""

from __future__ import annotations

import argparse
import os
import shutil
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from typing import Iterator


# VBox NAT puts the host at 10.0.2.2 from inside the guest. But VBox slirp
# *intercepts* UDP/69 to that alias for its built-in TFTP server when
# EnableTFTP=1 -- meaning external TFTP at 10.0.2.2 is unreachable from the
# guest. To actually hit our host-side listener, the next-server IP must be
# OUTSIDE the NAT alias range so slirp NATs the packet through to the real
# network. We use the host's LAN IP for that.
HOST_LAN_IP_DEFAULT = "192.168.25.13"

# Bootfile name we ask VBox to advertise. We do NOT have to actually serve
# anything for it — we just want to see whether the guest tries to fetch
# something from us.
DEFAULT_BOOTFILE = "fleetboot-smoke-marker.efi"

DEFAULT_VM_NAME = "fleetboot-vbox-smoke"
DEFAULT_LISTEN_PORT = 69
DEFAULT_WAIT_SECONDS = 90


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vm-name", default=DEFAULT_VM_NAME)
    parser.add_argument("--bootfile", default=DEFAULT_BOOTFILE)
    parser.add_argument("--port", type=int, default=DEFAULT_LISTEN_PORT)
    parser.add_argument(
        "--wait", type=int, default=DEFAULT_WAIT_SECONDS,
        help="seconds to wait for the RRQ before giving up",
    )
    parser.add_argument(
        "--keep-vm", action="store_true",
        help="don't unregister the VM after the run (for re-runs)",
    )
    return parser.parse_args(argv)


def need_vboxmanage() -> str:
    path = shutil.which("VBoxManage")
    if path is None:
        raise SystemExit(
            "VBoxManage not on PATH — install Oracle VirtualBox first."
        )
    return path


def run_vbox(*args: str, check: bool = True, capture: bool = False) -> subprocess.CompletedProcess:
    """Run VBoxManage and stream output unless `capture=True`."""
    cmd = ["VBoxManage", *args]
    print(f"vbox: {' '.join(cmd)}", flush=True)
    return subprocess.run(
        cmd, check=check, capture_output=capture, text=True
    )


def vm_exists(vm_name: str) -> bool:
    proc = run_vbox("list", "vms", capture=True)
    # Lines look like:  "fleetboot-vbox-smoke" {uuid}
    return f'"{vm_name}"' in proc.stdout


def destroy_vm(vm_name: str) -> None:
    """Power off and unregister a VM. Idempotent — failures are tolerated."""
    if not vm_exists(vm_name):
        return
    # Tolerate already-stopped VMs; this is best-effort cleanup.
    subprocess.run(
        ["VBoxManage", "controlvm", vm_name, "poweroff"],
        check=False, capture_output=True,
    )
    # Tiny wait so the VBox process actually releases the VM lock.
    time.sleep(1.0)
    subprocess.run(
        ["VBoxManage", "unregistervm", vm_name, "--delete"],
        check=False, capture_output=True,
    )


def create_vm(vm_name: str, bootfile: str) -> None:
    """Build the throwaway UEFI VM that will PXE-boot."""
    run_vbox("createvm", "--name", vm_name, "--ostype", "Other_64", "--register")
    # EFI firmware — this is the whole point of the test.
    run_vbox("modifyvm", vm_name, "--firmware", "efi64")
    run_vbox("modifyvm", vm_name, "--memory", "512", "--cpus", "1")
    # NAT networking with our DHCP override pointing the guest at the host.
    run_vbox("modifyvm", vm_name, "--nic1", "nat")
    run_vbox("modifyvm", vm_name, "--nattftpserver1", HOST_LAN_IP_DEFAULT)
    run_vbox("modifyvm", vm_name, "--nattftpfile1", bootfile)
    # VBox 7.x's NAT engine needs three extradata keys to actually advertise
    # bootp options in DHCP replies. The --nattftp* modifyvm options *write*
    # these to the VM XML, but in 7.2 the runtime NAT config only picks up
    # NextServer; BootFile and EnableTFTP must be set directly here for the
    # DHCP server to include them in the OFFER. Path is keyed by NIC device.
    nic_cfg = "VBoxInternal/Devices/virtio-net/0/LUN#0/Config"
    run_vbox("setextradata", vm_name, f"{nic_cfg}/EnableTFTP", "1")
    run_vbox("setextradata", vm_name, f"{nic_cfg}/BootFile", bootfile)
    run_vbox("setextradata", vm_name, f"{nic_cfg}/NextServer",
             HOST_LAN_IP_DEFAULT)
    # Boot from network only — no disk, no anything else.
    run_vbox("modifyvm", vm_name, "--boot1", "net", "--boot2", "none",
             "--boot3", "none", "--boot4", "none")
    # Headless friendly: virtio NIC is well-supported by EDK2 PXE.
    run_vbox("modifyvm", vm_name, "--nictype1", "virtio")
    # Serial console to a host file so we can see what UEFI / iPXE is doing
    # when it boots. Without this, headless leaves us blind to PXE failures.
    serial_path = f"/tmp/{vm_name}-serial.log"
    # Truncate any prior log so we only see this run.
    open(serial_path, "w").close()
    run_vbox("modifyvm", vm_name, "--uart1", "0x3F8", "4",
             "--uartmode1", "file", serial_path)
    print(f"smoke: serial log at {serial_path}", flush=True)


@contextmanager
def started_vm(vm_name: str) -> Iterator[None]:
    """Start the VM headless; ensure it stops even if the body raises."""
    run_vbox("startvm", vm_name, "--type", "headless")
    try:
        yield
    finally:
        subprocess.run(
            ["VBoxManage", "controlvm", vm_name, "poweroff"],
            check=False, capture_output=True,
        )


def bind_tftp_listener(port: int) -> socket.socket:
    """Bind a UDP socket on the host for the guest's TFTP RRQ to land on."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", port))
        sock.settimeout(0.5)
        return sock
    except PermissionError as exc:
        raise SystemExit(
            f"cannot bind UDP/{port}: {exc}.\n"
            "Run with sudo, or grant Python `cap_net_bind_service`:\n"
            "  sudo setcap cap_net_bind_service=+ep $(readlink -f $(which python3))\n"
            "Or pass --port 6969 for a debug-only run on an unprivileged port."
        ) from exc


def parse_rrq(packet: bytes) -> tuple[str, str] | None:
    """If `packet` is a TFTP RRQ, return (filename, mode)."""
    if len(packet) < 4:
        return None
    if int.from_bytes(packet[:2], "big") != 1:  # 1 == RRQ
        return None
    payload = packet[2:]
    nul = payload.find(b"\x00")
    if nul < 0:
        return None
    filename = payload[:nul].decode("ascii", errors="replace")
    rest = payload[nul + 1 :]
    nul2 = rest.find(b"\x00")
    if nul2 < 0:
        return None
    mode = rest[:nul2].decode("ascii", errors="replace")
    return filename, mode


def wait_for_rrq(sock: socket.socket, deadline_seconds: int, bootfile_hint: str) -> bool:
    """Block (with periodic timeout) until the deadline, returning True if we
    saw any TFTP request from any guest peer."""
    started_at = time.monotonic()
    print(
        f"smoke: listening for TFTP RRQ on 0.0.0.0:{sock.getsockname()[1]} "
        f"(expecting {bootfile_hint!r}) — up to {deadline_seconds}s",
        flush=True,
    )
    seen_any_traffic = False
    while time.monotonic() - started_at < deadline_seconds:
        try:
            packet, peer = sock.recvfrom(2048)
        except socket.timeout:
            continue
        seen_any_traffic = True
        print(f"smoke: received {len(packet)}B from {peer}", flush=True)
        parsed = parse_rrq(packet)
        if parsed is None:
            print(f"smoke:   non-RRQ packet: {packet!r}", flush=True)
            continue
        filename, mode = parsed
        print(
            f"smoke: SUCCESS — got RRQ for {filename!r} (mode={mode!r}) "
            f"from {peer}",
            flush=True,
        )
        return True
    if not seen_any_traffic:
        print("smoke: no UDP packets received on the listener", flush=True)
    return False


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    need_vboxmanage()

    print(
        f"smoke: bootfile={args.bootfile!r} listen_port={args.port} "
        f"wait={args.wait}s vm={args.vm_name!r}",
        flush=True,
    )

    listener = bind_tftp_listener(args.port)
    print(f"smoke: bound listener (uid={os.getuid()})", flush=True)

    destroy_vm(args.vm_name)
    create_vm(args.vm_name, args.bootfile)
    try:
        with started_vm(args.vm_name):
            ok = wait_for_rrq(listener, args.wait, args.bootfile)
    finally:
        listener.close()
        if not args.keep_vm:
            destroy_vm(args.vm_name)

    if ok:
        print(
            "\nsmoke verdict: VBox UEFI PXE DID reach our external listener.\n"
            "  -> The VBox path is viable. Next: wire it into make functional-test.",
            flush=True,
        )
        return 0

    print(
        "\nsmoke verdict: NO request reached us within the timeout.\n"
        "  -> VBox UEFI PXE may not honour external next-server here, OR the\n"
        "     guest could not bring up DHCP. Re-run with --keep-vm and check\n"
        "     in the VBox GUI; consider falling back to the QEMU bridge path.",
        flush=True,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
