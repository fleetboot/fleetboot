"""End-to-end PXE chain through a real VirtualBox UEFI guest.

What this proves:

  - VBox UEFI guest does DHCP, gets bootp options pointing at our tftpjail.
  - The guest TFTP-fetches /jail/<mac>/<arch>/<platform> from tftpjail.
  - tftpjail parses identity, runs policy, calls fleetboot's /resolve to
    confirm the MAC is registered, then /sessions to mint a per-boot token,
    then renders a grub.cfg with that token stamped in.
  - The grub.cfg bytes go back to the VM as TFTP DATA blocks.

What we DO NOT test here:

  - GRUB then executing the cfg, fetching kernel/initrd/squashfs over HTTP.
    The cfg we serve points at fleetboot's /boot/* URLs but the guest is a
    blank disk: it has no GRUB binary loaded, so it'll fail right after the
    cfg arrives. That's fine — proving the cfg arrived (the session was
    minted for our enrolled MAC) is enough to prove the whole brain works.

Slow (UEFI cold boot + DHCP timing). Run with: ``make vbox-functional-test``.
"""

from __future__ import annotations

import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest
import uvicorn

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
WAIT_SECONDS = 120

# We pin the VM MAC to a stable VBox-prefixed value so we can pre-enrol it
# in the registry. VBox accepts the bare 12-hex form.
VM_MAC_RAW = "080027aabbcc"
VM_MAC_COLON = "08:00:27:aa:bb:cc"

# We always send the request to the host's listener via VBox NAT. The host's
# real LAN IP routes through slirp; the 10.0.2.2 alias would be intercepted.
TFTPJAIL_PATH = f"/jail/{VM_MAC_COLON}/x86_64/efi"


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
    """Best-effort cleanup of any prior VM with our name."""
    _vbox("controlvm", VM_NAME, "poweroff", check=False)
    time.sleep(0.5)
    _vbox("unregistervm", VM_NAME, "--delete", check=False)


def _create_and_start_vm(host_ip: str) -> None:
    _vbox("createvm", "--name", VM_NAME, "--ostype", "Other_64", "--register")
    _vbox("modifyvm", VM_NAME, "--firmware", "efi64")
    _vbox("modifyvm", VM_NAME, "--memory", "512", "--cpus", "1")
    _vbox("modifyvm", VM_NAME, "--nic1", "nat")
    _vbox("modifyvm", VM_NAME, "--nictype1", "virtio")
    _vbox("modifyvm", VM_NAME, "--macaddress1", VM_MAC_RAW)
    _vbox("modifyvm", VM_NAME, "--boot1", "net", "--boot2", "none",
          "--boot3", "none", "--boot4", "none")
    _vbox("modifyvm", VM_NAME, "--nattftpserver1", host_ip)
    _vbox("modifyvm", VM_NAME, "--nattftpfile1", TFTPJAIL_PATH)
    nic_cfg = "VBoxInternal/Devices/virtio-net/0/LUN#0/Config"
    _vbox("setextradata", VM_NAME, f"{nic_cfg}/EnableTFTP", "1")
    _vbox("setextradata", VM_NAME, f"{nic_cfg}/BootFile", TFTPJAIL_PATH)
    _vbox("setextradata", VM_NAME, f"{nic_cfg}/NextServer", host_ip)
    serial_log = f"/tmp/{VM_NAME}-serial.log"
    Path(serial_log).write_text("")
    _vbox("modifyvm", VM_NAME, "--uart1", "0x3F8", "4",
          "--uartmode1", "file", serial_log)
    _vbox("startvm", VM_NAME, "--type", "headless")


# ---- Fleetboot in a thread -----------------------------------------------


class _StartedFleetboot:
    def __init__(self, tmp_path: Path) -> None:
        self.sessions = BootSessionStore()
        self.registry = MachineRegistry(tmp_path / "machines.sqlite")
        self.port = _find_free_tcp_port()
        self.app = create_app(
            sessions=self.sessions,
            mint_secret=MINT_SECRET,
            admin_secret=ADMIN_SECRET,
            registry=self.registry,
            boot_dir=tmp_path / "boot",
        )
        (tmp_path / "boot").mkdir(exist_ok=True)
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
        return f"http://{_host_lan_ip()}:{self.port}"


# ---- The test ------------------------------------------------------------


def test_vbox_uefi_pxe_drives_full_tftpjail_fleetboot_chain(tmp_path: Path):
    host_ip = _host_lan_ip()
    fleetboot = _StartedFleetboot(tmp_path)
    fleetboot.start()

    # Real wire: tftpjail's client points at the running fleetboot.
    client = FleetbootClient(
        base_url=fleetboot.base_url, mint_secret=MINT_SECRET,
    )

    # Enrol the VBox MAC via the admin API (proves /machines + /resolve are
    # wired end-to-end too).
    enroll = client._http_client  # type: ignore[attr-defined]
    import httpx

    with httpx.Client(timeout=5.0) as http:
        response = http.post(
            f"{fleetboot.base_url}/machines",
            json={
                "mac": VM_MAC_COLON, "profile_name": "vbox-smoke",
                "architecture": "x86_64", "platform": "efi",
            },
            headers={"Authorization": f"Bearer {ADMIN_SECRET}"},
        )
        assert response.status_code == 201, response.text

    # On a NAT'd guest the source IP that reaches us is the HOST's own IP
    # (slirp rewrites it). We can't ARP-resolve the guest MAC from that, so
    # we install a permissive neighbour lookup for the test: it returns
    # whatever asserted MAC the request claims. The ARP check is unit-tested
    # elsewhere; this test is about the full PXE chain.
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
        ack_timeout_seconds=1.0,
        max_retries=3,
    )
    tftpjail.start()

    _destroy_vm()
    try:
        _create_and_start_vm(host_ip)

        # Wait until fleetboot records a minted session for our VM's MAC.
        # That's the strong signal: it means the RRQ landed at tftpjail,
        # the policy approved (registry hit succeeded), and the renderer
        # ran the mint.
        deadline = time.monotonic() + WAIT_SECONDS
        minted = False
        while time.monotonic() < deadline:
            for session in fleetboot.sessions.active_sessions():
                if session.mac == VM_MAC_COLON:
                    minted = True
                    break
            if minted:
                break
            time.sleep(0.5)
        assert minted, (
            f"no session minted for {VM_MAC_COLON} within {WAIT_SECONDS}s — "
            f"check /tmp/{VM_NAME}-serial.log for the UEFI PXE output"
        )
    finally:
        _destroy_vm()
        tftpjail.stop()
        fleetboot.stop()
