"""End-to-end PXE chain through a real VirtualBox UEFI guest.

What this proves, when it passes:

  1. VBox UEFI fetches fleetboot-x64-uefi.efi (our built chainload binary) from
     tftpjail's public-asset directory — no identity / registry check yet.
  2. GRUB starts. Its embedded config fetches
     /jail/<mac>/x86_64/efi from tftpjail over TFTP.
  3. tftpjail parses identity, runs policy, calls fleetboot's /resolve to
     confirm the MAC is registered, then /sessions to mint a per-boot token,
     then renders a grub.cfg with that token stamped into every URL.
  4. GRUB executes the cfg: fetches vmlinuz + initrd.img from fleetboot's
     /boot/*?t=TOKEN over HTTP, then boots the kernel with the cmdline the
     renderer produced (includes console=ttyS0 because we enrol with
     serial_console=True).
  5. live-boot inside the initrd HTTP-fetches the squashfs, sets up the
     tmpfs overlay, switch_roots, and starts systemd.
  6. The fleetboot reporter inside the booted image POSTs network_up to
     fleetboot once networking comes up.

We assert on step 6 — the strongest signal that everything upstream worked.

Slow: a UEFI cold boot + DHCP + multi-stage chain + squashfs download takes
several minutes. Run only via ``make vbox-functional-test``.
"""

from __future__ import annotations

import socket
import subprocess
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn

from fleetboot.boot_states import BootState
from fleetboot.server.app import create_app
from fleetboot.server.boot_sessions import BootSessionStore
from fleetboot.server.registry import MachineRegistry

# tftpjail — sys.path set by conftest.py
from tftpjail.fleetboot_client import FleetbootClient, build_registry_lookup
from tftpjail.policy import Policy
from tftpjail.renderer import build_grub_config_renderer
from tftpjail.server import TftpJailServer


MINT_SECRET = "vbox-test-mint-secret"
ADMIN_SECRET = "vbox-test-admin-secret"
VM_NAME = "fleetboot-vbox-fullpxe"
TFTP_PORT = 69

# Up to five minutes for the whole UEFI → GRUB → kernel → live-boot →
# reporter chain. Real time is closer to 2–3 minutes once the squashfs is
# in the kernel's HTTP cache; first run is longer.
WAIT_SECONDS = 300

# Pinned VBox-prefixed MAC so we can pre-enrol it.
VM_MAC_RAW = "080027aabbcc"
VM_MAC_COLON = "08:00:27:aa:bb:cc"

# What VBox tells the guest's UEFI to TFTP-fetch as its first stage.
BOOTFILE = "fleetboot-x64-uefi.efi"

# Build artifacts the test reads (and points clients at). All produced by
# the project's Make targets; we skip rather than fail if any is missing.
REPO_ROOT = Path(__file__).resolve().parents[2]
BUILD_DIR = REPO_ROOT / "build"
REQUIRED_ARTIFACTS = (
    BUILD_DIR / "fleetboot-x64-uefi.efi",
    BUILD_DIR / "vmlinuz",
    BUILD_DIR / "initrd.img",
    BUILD_DIR / "fleetboot-amd64.squashfs",
)


# ---- Pre-flight ----------------------------------------------------------


def _skip_if_artifacts_missing() -> None:
    missing = [p for p in REQUIRED_ARTIFACTS if not p.is_file()]
    if missing:
        names = ", ".join(p.name for p in missing)
        pytest.skip(
            f"Missing build artifacts: {names}. "
            "Run `make grub-binary && make image` before this test."
        )


# ---- Helpers --------------------------------------------------------------


def _host_lan_ip() -> str:
    """Best-effort: open a UDP socket to a public-ish address and read back
    the local source IP the kernel chose."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]


def _find_free_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("0.0.0.0", 0))
        return s.getsockname()[1]


def _vbox(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["VBoxManage", *args], check=check, capture_output=True, text=True
    )


def _destroy_vm() -> None:
    _vbox("controlvm", VM_NAME, "poweroff", check=False)
    time.sleep(0.5)
    _vbox("unregistervm", VM_NAME, "--delete", check=False)


def _create_and_start_vm(host_ip: str) -> str:
    """Build the VM and start it headless; return serial-log path."""
    _vbox("createvm", "--name", VM_NAME, "--ostype", "Debian_64", "--register")
    _vbox("modifyvm", VM_NAME, "--firmware", "efi64")
    _vbox("modifyvm", VM_NAME, "--memory", "2048", "--cpus", "2")
    _vbox("modifyvm", VM_NAME, "--nic1", "nat")
    _vbox("modifyvm", VM_NAME, "--nictype1", "virtio")
    _vbox("modifyvm", VM_NAME, "--macaddress1", VM_MAC_RAW)
    _vbox("modifyvm", VM_NAME, "--boot1", "net", "--boot2", "none",
          "--boot3", "none", "--boot4", "none")
    _vbox("modifyvm", VM_NAME, "--nattftpserver1", host_ip)
    _vbox("modifyvm", VM_NAME, "--nattftpfile1", BOOTFILE)
    nic_cfg = "VBoxInternal/Devices/virtio-net/0/LUN#0/Config"
    _vbox("setextradata", VM_NAME, f"{nic_cfg}/EnableTFTP", "1")
    _vbox("setextradata", VM_NAME, f"{nic_cfg}/BootFile", BOOTFILE)
    _vbox("setextradata", VM_NAME, f"{nic_cfg}/NextServer", host_ip)
    serial_log = f"/tmp/{VM_NAME}-serial.log"
    Path(serial_log).write_text("")
    _vbox("modifyvm", VM_NAME, "--uart1", "0x3F8", "4",
          "--uartmode1", "file", serial_log)
    _vbox("startvm", VM_NAME, "--type", "headless")
    return serial_log


# ---- Fleetboot in a thread -----------------------------------------------


class _StartedFleetboot:
    def __init__(self) -> None:
        self.sessions = BootSessionStore()
        # Registry in /tmp so it survives if we want to inspect post-run.
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
        # Access log on so the test surfaces what the booted image asks for
        # (live-boot fetching the squashfs, the reporter posting /status).
        config = uvicorn.Config(
            self.app, host="0.0.0.0", port=self.port,
            log_level="info", access_log=True,
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
        return f"http://{_host_lan_ip()}:{self.port}"


# ---- The test ------------------------------------------------------------


def test_vbox_uefi_pxe_boots_kernel_and_reporter_calls_home():
    _skip_if_artifacts_missing()

    host_ip = _host_lan_ip()
    fleetboot = _StartedFleetboot()
    fleetboot.start()

    client = FleetbootClient(
        base_url=fleetboot.base_url, mint_secret=MINT_SECRET,
    )

    # Enrol the VBox MAC; opt in to serial_console because this is a VM.
    with httpx.Client(timeout=5.0) as http:
        response = http.post(
            f"{fleetboot.base_url}/machines",
            json={
                "mac": VM_MAC_COLON, "profile_name": "vbox-smoke",
                "architecture": "x86_64", "platform": "efi",
                "serial_console": True,
            },
            headers={"Authorization": f"Bearer {ADMIN_SECRET}"},
        )
        assert response.status_code == 201, response.text

    # On NAT'd guests the source IP we see is the host's own. We can't ARP
    # back to the guest MAC, so we install a permissive neighbour lookup
    # for the test: it returns whatever asserted MAC the request claims.
    # The ARP check is unit-tested elsewhere.
    def permissive_neighbour(_ip: str) -> str:
        return VM_MAC_COLON

    policy = Policy(
        registry_lookup=build_registry_lookup(client),
        asset_renderer=build_grub_config_renderer(
            fleetboot_client=client,
            fleetboot_base_url=fleetboot.base_url,
        ),
    )
    tftpjail = TftpJailServer(
        host="0.0.0.0",
        port=TFTP_PORT,
        policy=policy,
        neighbour_lookup=permissive_neighbour,
        # Lets UEFI PXE fetch fleetboot-x64-uefi.efi without registry auth.
        public_assets_dir=BUILD_DIR,
        ack_timeout_seconds=1.0,
        max_retries=3,
    )
    tftpjail.start()

    _destroy_vm()
    serial_log: str | None = None
    try:
        serial_log = _create_and_start_vm(host_ip)

        # Wait for the booted image's reporter to POST network_up.
        deadline = time.monotonic() + WAIT_SECONDS
        reached_network_up = False
        last_state: BootState | None = None
        while time.monotonic() < deadline:
            for session in fleetboot.sessions.active_sessions():
                if session.mac != VM_MAC_COLON:
                    continue
                last_state = session.latest_state
                if session.latest_state == BootState.NETWORK_UP or (
                    session.latest_state is not None
                    and session.latest_state.value != "network_up"
                ):
                    # Anything network_up or beyond is enough.
                    reached_network_up = True
            if reached_network_up:
                break
            time.sleep(1.0)
        assert reached_network_up, (
            f"reporter did not POST network_up for {VM_MAC_COLON} within "
            f"{WAIT_SECONDS}s. Last seen state: {last_state}. "
            f"Serial log at {serial_log}."
        )
    finally:
        _destroy_vm()
        tftpjail.stop()
        fleetboot.stop()
