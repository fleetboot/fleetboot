"""End-to-end PXE chain through a libvirt-managed QEMU UEFI guest.

What this proves, on success:

  1. libvirt's dnsmasq on virbr-fbt advertises our bootfile + next-server.
  2. QEMU UEFI fetches fleetboot-x64-uefi from tftpjail via TFTP.
  3. GRUB's embedded config fetches /jail/<mac>/x86_64/efi via TFTP.
  4. tftpjail mints a token, renders the per-MAC grub.cfg.
  5. GRUB TFTP-fetches vmlinuz + initrd.img from tftpjail's public assets.
  6. Kernel boots; live-boot wgets the squashfs over HTTP from fleetboot.
  7. systemd starts; the fleetboot-network-up.service runs the reporter
     which POSTs network_up to fleetboot.

Same shape as the VBox functional test, but using libvirt's isolated bridge
instead of VBox NAT. KVM stays loaded throughout; no module dance.

Slow: cold UEFI boot through full chain takes 30–90 s.
"""

from __future__ import annotations

import socket
import subprocess
import threading
import time
from pathlib import Path
from textwrap import dedent

import httpx
import pytest
import uvicorn

from fleetboot.boot_states import BootState
from fleetboot.server.app import create_app
from fleetboot.server.boot_sessions import BootSessionStore
from fleetboot.server.registry import MachineRegistry

# tftpjail (sys.path set in conftest)
from tftpjail.fleetboot_client import FleetbootClient, build_registry_lookup
from tftpjail.policy import Policy
from tftpjail.renderer import build_grub_config_renderer
from tftpjail.server import TftpJailServer


# ---- Constants ------------------------------------------------------------


MINT_SECRET = "qemu-test-mint-secret"
ADMIN_SECRET = "qemu-test-admin-secret"

NETWORK_NAME = "fleetboot"
HOST_IP_ON_BRIDGE = "192.168.99.1"
VM_NAME = "fleetboot-qemu-fullpxe"
VM_MAC = "52:54:00:fb:ee:01"

WAIT_SECONDS = 240
# The "current" serial log path is a stable name in /tmp so a human can
# `tail` it during boot, but the actual per-run file gets a timestamp
# suffix so a fresh test run can replace it without needing root to
# unlink the previous (libvirt writes the file as root:root mode 0600).
# The stable path becomes a symlink to the latest per-run log.
SERIAL_LOG = f"/tmp/{VM_NAME}-serial.log"

REPO_ROOT = Path(__file__).resolve().parents[2]
BUILD_DIR = REPO_ROOT / "build"
NETWORK_XML = REPO_ROOT / "image" / "libvirt-fleetboot-network.xml"

REQUIRED_ARTIFACTS = (
    BUILD_DIR / "fleetboot-x64-uefi",
    BUILD_DIR / "vmlinuz",
    BUILD_DIR / "initrd.img",
    BUILD_DIR / "fleetboot-cinnamon-desktop-amd64.squashfs",
)


# ---- Helpers --------------------------------------------------------------


def _virsh(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    """Run virsh under the libvirt group, against the SYSTEM libvirtd.

    Two non-obvious bits:

    * `sg libvirt -c …` makes the call inherit the libvirt group even when
      the current shell session pre-dates the user being added to it.
    * Explicit `-c qemu:///system` defeats virsh's default of falling back
      to `qemu:///session` (the unprivileged URI) when not running as root.
      Session-mode libvirtd cannot create kernel bridges; we need system
      mode for that.
    """
    inner = "virsh -c qemu:///system " + " ".join(args)
    cmd = ["sg", "libvirt", "-c", inner]
    return subprocess.run(cmd, check=check, capture_output=True, text=True)


def _find_free_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("0.0.0.0", 0))
        return s.getsockname()[1]


def _ensure_network() -> None:
    """Define + start the fleetboot network if it isn't already running."""
    listed = _virsh("net-list", "--all", "--name", check=False).stdout.split()
    if NETWORK_NAME not in listed:
        _virsh("net-define", str(NETWORK_XML))
    state = _virsh("net-info", NETWORK_NAME, check=False).stdout
    if "Active:         yes" not in state:
        _virsh("net-start", NETWORK_NAME)


def _destroy_vm(name: str) -> None:
    _virsh("destroy", name, check=False)
    # Transient domains vanish on destroy; defined ones need undefine.
    _virsh("undefine", name, "--nvram", check=False)


def _prepare_serial_log(tmp_path: Path) -> str:
    """Allocate a fresh serial-log path that matt can read after the run.

    libvirt writes the serial-log file as root:root mode 0600. Per-run path
    sidesteps the "previous file owned by root, matt cannot replace it"
    trap, and we pre-create it world-writable so libvirt can still write
    while matt can read. We also update the stable `SERIAL_LOG` symlink
    so a human can `tail` from one well-known place across runs.
    """
    per_run = tmp_path / "serial.log"
    per_run.write_text("")
    per_run.chmod(0o666)
    stable = Path(SERIAL_LOG)
    if stable.is_symlink() or stable.exists():
        try:
            stable.unlink()
        except PermissionError:
            # Stale root-owned file from a pre-symlink test; leave it,
            # the per-run file is still where libvirt and we both read it.
            pass
    try:
        stable.symlink_to(per_run)
    except (PermissionError, FileExistsError):
        pass
    return str(per_run)


def _domain_xml(serial_log_path: str) -> str:
    """Build the libvirt domain XML for the throwaway PXE-boot guest."""
    return dedent(
        f"""\
        <domain type='kvm'>
          <name>{VM_NAME}</name>
          <memory unit='MiB'>2048</memory>
          <vcpu>2</vcpu>
          <os firmware='efi'>
            <!-- libvirt picks an OVMF descriptor based on these feature
                 flags. With the defaults we'd get the Secure Boot variant
                 and our self-built (unsigned) fleetboot-x64-uefi would be
                 rejected with "access denied" at PXE. Explicitly off. -->
            <firmware>
              <feature enabled='no' name='secure-boot'/>
              <feature enabled='no' name='enrolled-keys'/>
            </firmware>
            <type arch='x86_64' machine='q35'>hvm</type>
            <!-- No os/boot here; the per-interface <boot order='1'/> below
                 picks the boot device. libvirt rejects mixing the two. -->
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
              <mac address='{VM_MAC}'/>
              <model type='virtio'/>
              <boot order='1'/>
            </interface>
            <serial type='file'>
              <source path='{serial_log_path}'/>
              <target port='0'/>
            </serial>
            <console type='file'>
              <source path='{serial_log_path}'/>
              <target type='serial' port='0'/>
            </console>
            <!-- No graphics device: omit the element entirely. libvirt
                 rejects `<graphics type='none'/>` as malformed. -->
          </devices>
        </domain>
        """
    )


# ---- Fleetboot in a thread -----------------------------------------------


class _StartedFleetboot:
    def __init__(self) -> None:
        self.sessions = BootSessionStore()
        self.registry = MachineRegistry(
            f"/tmp/{VM_NAME}-machines.sqlite"
        )
        self.port = _find_free_tcp_port()
        self.app = create_app(
            sessions=self.sessions,
            mint_secret=MINT_SECRET,
            admin_secret=ADMIN_SECRET,
            registry=self.registry,
            boot_dir=BUILD_DIR,
        )
        config = uvicorn.Config(
            self.app, host="0.0.0.0", port=self.port,
            log_level="warning", access_log=False,
        )
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)

    def start(self) -> None:
        self._thread.start()
        for _ in range(200):
            if self._server.started:
                return
            time.sleep(0.05)
        raise RuntimeError("fleetboot failed to come up")

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=5)

    @property
    def base_url(self) -> str:
        return f"http://{HOST_IP_ON_BRIDGE}:{self.port}"


# ---- The test ------------------------------------------------------------


def _skip_if_artifacts_missing() -> None:
    missing = [p for p in REQUIRED_ARTIFACTS if not p.is_file()]
    if missing:
        names = ", ".join(p.name for p in missing)
        pytest.skip(
            f"Missing build artifacts: {names}. "
            "Run `make grub-binary && make image` before this test."
        )


def test_qemu_uefi_pxe_boots_kernel_and_reporter_calls_home(tmp_path: Path):
    _skip_if_artifacts_missing()
    _ensure_network()

    fleetboot = _StartedFleetboot()
    fleetboot.start()

    client = FleetbootClient(
        base_url=fleetboot.base_url, mint_secret=MINT_SECRET,
    )

    with httpx.Client(timeout=5.0) as http:
        # Enrol the VM with the `cinnamon-desktop` example profile so the rendered grub.cfg
        # asks for `fleetboot-cinnamon-desktop-amd64.squashfs` — proves the profile
        # mechanism all the way from registry to live-boot.
        response = http.post(
            f"{fleetboot.base_url}/machines",
            json={
                "mac": VM_MAC, "profile_name": "cinnamon-desktop",
                "architecture": "x86_64", "platform": "efi",
                "serial_console": True,
            },
            headers={"Authorization": f"Bearer {ADMIN_SECRET}"},
        )
        assert response.status_code == 201, response.text

    # Real ARP would work here because the guest is on our bridge (no NAT),
    # but we don't actually read the host's neighbour table from the test.
    # A permissive lookup keeps the test focused on the PXE chain itself.
    def permissive_neighbour(_ip: str) -> str:
        return VM_MAC

    policy = Policy(
        registry_lookup=build_registry_lookup(client),
        asset_renderer=build_grub_config_renderer(
            fleetboot_client=client,
            fleetboot_base_url=fleetboot.base_url,
        ),
    )
    tftpjail = TftpJailServer(
        host="0.0.0.0",
        port=69,
        policy=policy,
        neighbour_lookup=permissive_neighbour,
        public_assets_dir=BUILD_DIR,
        ack_timeout_seconds=1.0,
        max_retries=3,
    )
    tftpjail.start()

    _destroy_vm(VM_NAME)
    domain_xml_path = tmp_path / "domain.xml"
    serial_log_path = _prepare_serial_log(tmp_path)
    domain_xml_path.write_text(_domain_xml(serial_log_path))
    try:
        _virsh("create", str(domain_xml_path))

        deadline = time.monotonic() + WAIT_SECONDS
        reached_network_up = False
        while time.monotonic() < deadline:
            for session in fleetboot.sessions.active_sessions():
                if session.mac == VM_MAC and (
                    session.latest_state == BootState.NETWORK_UP
                    or (
                        session.latest_state is not None
                        and session.latest_state.value != "network_up"
                    )
                ):
                    reached_network_up = True
                    break
            if reached_network_up:
                break
            time.sleep(1.0)
        assert reached_network_up, (
            f"reporter did not POST network_up within {WAIT_SECONDS}s. "
            f"Serial log: {SERIAL_LOG}"
        )
    finally:
        _destroy_vm(VM_NAME)
        tftpjail.stop()
        fleetboot.stop()
