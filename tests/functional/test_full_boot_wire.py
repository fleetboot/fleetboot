"""End-to-end functional test of the fleetboot ↔ tftpjail wire.

We start a real fleetboot FastAPI server in-process (via uvicorn on a
background thread) and a real tftpjail UDP server (also in-process). A tiny
inline TFTP client then drives the full real boot-policy flow:

    Client TFTP-RRQ /jail/<mac>/<arch>/<platform>
        -> tftpjail parses identity, runs policy
        -> tftpjail calls fleetboot POST /sessions (auth: shared secret)
        -> fleetboot mints a per-boot token bound to <mac>
        -> tftpjail renders grub.cfg stamping the token into every URL
        -> tftpjail sends the rendered grub.cfg over TFTP
    Client (acting as GRUB/live-boot) HTTP-GET /boot/vmlinuz?t=<token>
        -> fleetboot validates token, serves bytes
    Client (acting as the in-image reporter) POST /status with token
        -> fleetboot records network_up

Same wire format the real fleet will use — except we drive the wire from
Python instead of QEMU. The QEMU image-smoke test proves the in-image side
separately.
"""

from __future__ import annotations

import socket
import threading
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn

from fleetboot.server.app import create_app
from fleetboot.server.boot_sessions import BootSessionStore


# tftpjail imports — usable because conftest.py prepended its path.
from tftpjail.identity import MacConsistency  # noqa: E402
from tftpjail.fleetboot_client import FleetbootClient  # noqa: E402
from tftpjail.policy import Policy  # noqa: E402
from tftpjail.protocol import OPCODE_DATA, OPCODE_ERROR, OPCODE_READ_REQUEST  # noqa: E402
from tftpjail.renderer import build_grub_config_renderer  # noqa: E402
from tftpjail.server import TftpJailServer  # noqa: E402
from tftpjail.transfer import build_ack_packet, parse_data_packet  # noqa: E402


MINT_SECRET = "shared-secret-for-tftpjail"
CLIENT_MAC = "aa:bb:cc:dd:ee:ff"


# ---- Inline TFTP client (small enough to duplicate; avoids cross-imports)


def _tftp_read(server_host: str, server_port: int, request_path: str) -> bytes:
    """Drive a complete RRQ → DATA/ACK loop and return the assembled bytes."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    sock.settimeout(3.0)
    try:
        rrq = (
            OPCODE_READ_REQUEST.to_bytes(2, "big")
            + request_path.encode("ascii")
            + b"\x00"
            + b"octet"
            + b"\x00"
        )
        sock.sendto(rrq, (server_host, server_port))
        body = bytearray()
        server_addr: tuple[str, int] | None = None
        expected_block = 1
        while True:
            packet, source = sock.recvfrom(2048)
            opcode = int.from_bytes(packet[:2], "big")
            if opcode == OPCODE_ERROR:
                code = int.from_bytes(packet[2:4], "big")
                msg = packet[4:].rstrip(b"\x00").decode("ascii", errors="replace")
                raise AssertionError(f"TFTP error {code}: {msg}")
            if opcode != OPCODE_DATA:
                continue
            block_number, block_data = parse_data_packet(packet)
            if server_addr is None:
                server_addr = source
            if block_number != expected_block:
                continue
            body.extend(block_data)
            sock.sendto(build_ack_packet(block_number), source)
            if len(block_data) < 512:
                return bytes(body)
            expected_block = (expected_block + 1) & 0xFFFF
    finally:
        sock.close()


# ---- Helpers --------------------------------------------------------------


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("0.0.0.0", 0))
        return s.getsockname()[1]


def _matching_neighbour(mac: str):
    def _lookup(_ip: str) -> str:
        return mac
    return _lookup


def _registry_with(known_mac: str):
    def _lookup(mac: str) -> Any | None:
        return "default-profile" if mac == known_mac else None
    return _lookup


class _StartedFleetboot:
    """Real fleetboot app running on a background uvicorn thread."""

    def __init__(self, boot_dir: Path) -> None:
        self.sessions = BootSessionStore()
        self.port = _find_free_port()
        self.app = create_app(
            sessions=self.sessions,
            mint_secret=MINT_SECRET,
            boot_dir=boot_dir,
        )
        config = uvicorn.Config(
            self.app,
            host="0.0.0.0",
            port=self.port,
            log_level="warning",
            access_log=False,
        )
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)

    def start(self) -> None:
        self._thread.start()
        for _ in range(200):
            if self._server.started:
                return
            threading.Event().wait(0.05)
        raise RuntimeError("fleetboot failed to come up")

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=5)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


@pytest.fixture
def boot_dir(tmp_path: Path) -> Path:
    """Populate a fake boot dir with all the allowlisted artifact names."""
    boot = tmp_path / "boot"
    boot.mkdir()
    (boot / "vmlinuz").write_bytes(b"\x7fELF-fake-kernel-bytes")
    (boot / "initrd.img").write_bytes(b"fake-initrd-bytes")
    (boot / "fleetboot-amd64.squashfs").write_bytes(b"fake-squashfs-bytes")
    return boot


@pytest.fixture
def stack(boot_dir: Path):
    """Bring up fleetboot + tftpjail; tear them down at the end of the test."""
    fleetboot = _StartedFleetboot(boot_dir=boot_dir)
    fleetboot.start()

    client = FleetbootClient(
        base_url=fleetboot.base_url, mint_secret=MINT_SECRET
    )
    renderer = build_grub_config_renderer(
        fleetboot_client=client,
        fleetboot_base_url=fleetboot.base_url,
    )
    policy = Policy(
        registry_lookup=_registry_with(CLIENT_MAC),
        asset_renderer=renderer,
    )
    tftpjail_server = TftpJailServer(
        host="127.0.0.1",
        port=0,
        policy=policy,
        neighbour_lookup=_matching_neighbour(CLIENT_MAC),
        ack_timeout_seconds=0.5,
        max_retries=3,
    )
    tftpjail_server.start()
    try:
        yield fleetboot, tftpjail_server
    finally:
        tftpjail_server.stop()
        fleetboot.stop()


# ---- The tests ------------------------------------------------------------


def test_grub_config_round_trips_with_minted_token(stack):
    """The headline test: client TFTPs the identity path, gets a real
    grub.cfg back with a real minted token stamped into every URL."""
    fleetboot, tftpjail_server = stack

    body = _tftp_read(
        server_host="127.0.0.1",
        server_port=tftpjail_server.bound_port,
        request_path=f"/jail/{CLIENT_MAC}/x86_64/efi",
    )

    text = body.decode("utf-8")
    assert "linux " in text
    assert "initrd " in text
    assert "boot\n" in text
    # The token shows up three places: kernel URL, initrd URL, fetch URL —
    # plus once in fleetboot.boot_token= on the kernel cmdline.
    # We don't know its value, but it must be a 64-char hex string and used
    # consistently in every URL the client will hit.
    import re

    tokens = re.findall(r"[?&]t=([0-9a-f]+)", text)
    assert tokens, f"no token query params found in:\n{text}"
    assert len(set(tokens)) == 1, "token should be the same across all URLs"
    minted_token = tokens[0]
    assert len(minted_token) >= 64
    # The same token should appear on the kernel cmdline.
    assert f"fleetboot.boot_token={minted_token}" in text
    # And the minted session should be live in fleetboot's store, bound to
    # our MAC.
    session = fleetboot.sessions.lookup(minted_token)
    assert session is not None
    assert session.mac == CLIENT_MAC


def test_boot_assets_served_using_minted_token(stack):
    """After the grub.cfg is delivered, the URLs it points at must work."""
    fleetboot, tftpjail_server = stack
    body = _tftp_read(
        server_host="127.0.0.1",
        server_port=tftpjail_server.bound_port,
        request_path=f"/jail/{CLIENT_MAC}/x86_64/efi",
    )
    import re

    token = re.search(r"[?&]t=([0-9a-f]+)", body.decode("utf-8")).group(1)

    with httpx.Client(timeout=5.0) as client:
        for name, expected in [
            ("vmlinuz", b"\x7fELF-fake-kernel-bytes"),
            ("initrd.img", b"fake-initrd-bytes"),
            ("fleetboot-amd64.squashfs", b"fake-squashfs-bytes"),
        ]:
            response = client.get(
                f"{fleetboot.base_url}/boot/{name}?t={token}"
            )
            assert response.status_code == 200, name
            assert response.content == expected, name


def test_status_post_with_minted_token_is_accepted(stack):
    """Closing the loop: the reporter inside the image uses the same token."""
    fleetboot, tftpjail_server = stack
    body = _tftp_read(
        server_host="127.0.0.1",
        server_port=tftpjail_server.bound_port,
        request_path=f"/jail/{CLIENT_MAC}/x86_64/efi",
    )
    import re

    token = re.search(r"[?&]t=([0-9a-f]+)", body.decode("utf-8")).group(1)

    with httpx.Client(timeout=5.0) as client:
        response = client.post(
            f"{fleetboot.base_url}/status",
            json={"state": "network_up"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 200
        assert response.json()["mac"] == CLIENT_MAC


def test_unknown_mac_path_gets_uniform_deny(stack):
    """An RRQ for an unregistered MAC must look identical to other denies."""
    _fleetboot, tftpjail_server = stack
    with pytest.raises(AssertionError) as info:
        _tftp_read(
            server_host="127.0.0.1",
            server_port=tftpjail_server.bound_port,
            request_path="/jail/aa:bb:cc:dd:ee:00/x86_64/efi",
        )
    # The same "File not found" we use for every deny.
    assert "TFTP error 1" in str(info.value)


def test_path_probe_gets_uniform_deny(stack):
    """A non-/jail/... probe gets the same answer as a deny."""
    _fleetboot, tftpjail_server = stack
    with pytest.raises(AssertionError) as info:
        _tftp_read(
            server_host="127.0.0.1",
            server_port=tftpjail_server.bound_port,
            request_path="/etc/passwd",
        )
    assert "TFTP error 1" in str(info.value)
