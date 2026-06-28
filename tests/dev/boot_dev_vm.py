"""Boot a transient libvirt QEMU UEFI VM that talks to the running dev
fleetboot server. The VM will appear on the dashboard's machines page
and tick through boot states as it comes up.

  python3 -m tests.dev.boot_dev_vm                       # random MAC
  python3 -m tests.dev.boot_dev_vm --mac 52:54:00:de:00:01
  python3 -m tests.dev.boot_dev_vm --profile school

Expects:
  - `make run-server` is already running in another shell.
  - The libvirt `fleetboot` network is active (run_server doesn't manage it;
    see docs/admin-guide.md for the one-time `virsh net-define` step).
  - build/ has the kernel, initrd, squashfs for the requested profile.

The VM is *transient* — it disappears on Ctrl-C and is not persisted.
"""

from __future__ import annotations

import argparse
import secrets as _secrets
import subprocess
import sys
import time
from pathlib import Path
from textwrap import dedent
from typing import Optional

import httpx


REPO_ROOT = Path(__file__).resolve().parents[2]
DEV_DIR = REPO_ROOT / "build" / "dev"
SECRETS_FILE = DEV_DIR / "secrets.env"

DEFAULT_FLEETBOOT_URL = "http://localhost:8080"
NETWORK_NAME = "fleetboot"
HOST_IP_ON_BRIDGE = "192.168.99.1"


def _read_admin_secret() -> str:
    if not SECRETS_FILE.is_file():
        raise SystemExit(
            f"no {SECRETS_FILE} — start `make run-server` once to "
            "generate dev secrets."
        )
    for line in SECRETS_FILE.read_text().splitlines():
        if line.startswith("FLEETBOOT_ADMIN_SECRET="):
            return line.split("=", 1)[1].strip()
    raise SystemExit(
        f"FLEETBOOT_ADMIN_SECRET missing from {SECRETS_FILE}"
    )


def _generate_mac() -> str:
    """A 52:54:00:DE:xx:xx address — DE for "dev", to disambiguate from
    real fleet hardware in the registry."""
    suffix = _secrets.token_hex(2)
    return f"52:54:00:de:{suffix[:2]}:{suffix[2:]}"


def _virsh(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    cmd = ["sg", "libvirt", "-c", "virsh -c qemu:///system " + " ".join(args)]
    return subprocess.run(cmd, check=check, capture_output=True, text=True)


def _spice_port(vm_name: str) -> Optional[int]:
    """Query libvirt for the autoport'd SPICE display port. Returns None if
    SPICE isn't configured or hasn't bound yet."""
    try:
        result = _virsh("domdisplay", vm_name)
    except subprocess.CalledProcessError:
        return None
    # Format like `spice://127.0.0.1:5900`.
    for line in result.stdout.splitlines():
        line = line.strip()
        if line.startswith("spice://"):
            try:
                return int(line.rsplit(":", 1)[1])
            except (ValueError, IndexError):
                return None
    return None


def _vm_name(mac: str) -> str:
    """A name that's unique per MAC but legible."""
    tail = mac.replace(":", "")[-6:]
    return f"fleetboot-dev-{tail}"


def _domain_xml(
    name: str,
    mac: str,
    serial_log: str,
    *,
    display: bool = False,
) -> str:
    # With --display, add a SPICE socket + virtio-vga so virt-viewer can
    # attach. Without it the VM is serial-only — keeps the dashboard
    # workflow snappy on a remote SSH host.
    display_block = ""
    if display:
        display_block = (
            "<graphics type='spice' autoport='yes' listen='127.0.0.1'/>"
            "<video><model type='virtio' heads='1'/></video>"
            "<input type='tablet' bus='usb'/>"
            "<controller type='usb' model='qemu-xhci'/>"
        )
    return dedent(
        f"""\
        <domain type='kvm'>
          <name>{name}</name>
          <memory unit='MiB'>2048</memory>
          <vcpu>2</vcpu>
          <os firmware='efi'>
            <firmware>
              <feature enabled='no' name='secure-boot'/>
              <feature enabled='no' name='enrolled-keys'/>
            </firmware>
            <type arch='x86_64' machine='q35'>hvm</type>
          </os>
          <features><acpi/><apic/></features>
          <cpu mode='host-passthrough'/>
          <clock offset='utc'/>
          <on_poweroff>destroy</on_poweroff>
          <on_reboot>destroy</on_reboot>
          <on_crash>destroy</on_crash>
          <devices>
            <emulator>/usr/bin/qemu-system-x86_64</emulator>
            <interface type='network'>
              <source network='{NETWORK_NAME}'/>
              <mac address='{mac}'/>
              <model type='virtio'/>
              <boot order='1'/>
            </interface>
            <serial type='file'>
              <source path='{serial_log}'/>
              <target port='0'/>
            </serial>
            <console type='file'>
              <source path='{serial_log}'/>
              <target type='serial' port='0'/>
            </console>
            {display_block}
          </devices>
        </domain>
        """
    )


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mac", default=None,
                        help="MAC for the VM (default: auto-generated)")
    parser.add_argument("--profile", default="school")
    parser.add_argument("--architecture", default="x86_64")
    parser.add_argument(
        "--fleetboot-url", default=DEFAULT_FLEETBOOT_URL,
        help="URL of the running dev server (default http://localhost:8080)",
    )
    parser.add_argument(
        "--display", action="store_true",
        help=(
            "Attach a graphical display: adds SPICE + virtio-vga to the VM "
            "and spawns virt-viewer so a local user with $DISPLAY can see "
            "the desktop boot. Requires virt-viewer installed."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    admin_secret = _read_admin_secret()

    mac = (args.mac or _generate_mac()).lower()
    vm_name = _vm_name(mac)
    serial_log = f"/tmp/{vm_name}-serial.log"
    # Make the log world-readable upfront so users can `tail -f` it.
    Path(serial_log).write_text("")
    Path(serial_log).chmod(0o666)

    # Pre-flight: does fleetboot answer?
    try:
        with httpx.Client(timeout=2.0) as client:
            response = client.get(f"{args.fleetboot_url}/dashboard")
        if response.status_code not in (200, 401):
            raise RuntimeError(f"unexpected status {response.status_code}")
    except (httpx.HTTPError, RuntimeError) as err:
        raise SystemExit(
            f"fleetboot at {args.fleetboot_url} isn't responding "
            f"({err}). Start `make run-server` first."
        )

    # Enrol.
    print(f"enrolling {mac} as profile={args.profile} arch={args.architecture}")
    with httpx.Client(timeout=5.0) as http:
        response = http.post(
            f"{args.fleetboot_url}/machines",
            json={
                "mac": mac,
                "profile_name": args.profile,
                "architecture": args.architecture,
                "platform": "efi",
                "serial_console": True,
            },
            headers={"Authorization": f"Bearer {admin_secret}"},
        )
        if response.status_code != 201:
            raise SystemExit(
                f"enrol failed: {response.status_code} {response.text}"
            )

    # Boot.
    _virsh("destroy", vm_name, check=False)
    _virsh("undefine", vm_name, "--nvram", check=False)
    domain_xml_path = Path(f"/tmp/{vm_name}.xml")
    domain_xml_path.write_text(
        _domain_xml(vm_name, mac, serial_log, display=args.display)
    )
    _virsh("create", str(domain_xml_path))

    print()
    print(f"VM running:    {vm_name}")
    print(f"MAC:           {mac}")
    print(f"serial log:    tail -f {serial_log}")
    print(f"dashboard:     {args.fleetboot_url}/dashboard")
    viewer_proc: Optional[subprocess.Popen] = None
    if args.display:
        spice_port = _spice_port(vm_name)
        if spice_port:
            print(f"SPICE port:    {spice_port} (bound 127.0.0.1)")
            print(
                f"SSH tunnel:    ssh -L {spice_port}:127.0.0.1:{spice_port} "
                "<this-host>"
            )
            print(
                f"then on your laptop:  remote-viewer "
                f"spice://127.0.0.1:{spice_port}"
            )
        else:
            print("SPICE port:    (not yet bound; try again in a second)")
        # Spawn virt-viewer if there's an X display to render to. Without
        # one, the SSH workflow above is the path.
        import os as _os
        if _os.environ.get("DISPLAY"):
            try:
                viewer_proc = subprocess.Popen(
                    ["virt-viewer", "--connect", "qemu:///system", vm_name],
                )
                print(f"virt-viewer:   pid {viewer_proc.pid}")
            except FileNotFoundError:
                print(
                    "virt-viewer not found; use the SSH tunnel above",
                    file=sys.stderr,
                )
        else:
            print(
                "(no $DISPLAY on this host — use the SSH tunnel above)"
            )
    print()
    print("press Ctrl-C to power off and clean up")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nshutting down VM...")
    finally:
        if viewer_proc is not None:
            viewer_proc.terminate()
        _virsh("destroy", vm_name, check=False)
        try:
            domain_xml_path.unlink()
        except FileNotFoundError:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
