"""Run fleetboot + tftpjail with the dashboard, for interactive dev use.

  python3 -m tests.dev.run_server          # listens on 0.0.0.0:8080
  python3 -m tests.dev.run_server --port 9000

Listens on the host so the libvirt-bridge VMs can reach it at
192.168.99.1:<port> AND a browser on the host can hit
http://localhost:<port>/dashboard. Secrets are generated on first run
and persisted; the machine registry survives between runs.

Run alongside `make boot-dev-vm` from another shell to actually see
something tick through the dashboard.
"""

from __future__ import annotations

import argparse
import secrets as _secrets
import signal
import socket
import subprocess
import sys
import threading
from pathlib import Path
from typing import Optional

import uvicorn

REPO_ROOT = Path(__file__).resolve().parents[2]
TFTPJAIL_ROOT = REPO_ROOT.parent / "tftpjail"

sys.path.insert(0, str(TFTPJAIL_ROOT))

from fleetboot.server.app import create_app  # noqa: E402
from fleetboot.server.boot_sessions import BootSessionStore  # noqa: E402
from fleetboot.server.registry import MachineRegistry  # noqa: E402

from tftpjail.fleetboot_client import (  # noqa: E402
    FleetbootClient,
    build_registry_lookup,
)
from tftpjail.policy import Policy  # noqa: E402
from tftpjail.renderer import build_grub_config_renderer  # noqa: E402
from tftpjail.server import TftpJailServer  # noqa: E402


DEV_DIR = REPO_ROOT / "build" / "dev"
SECRETS_FILE = DEV_DIR / "secrets.env"
REGISTRY_PATH = DEV_DIR / "machines.sqlite"
BOOT_DIR = REPO_ROOT / "build"

# IP the libvirt-managed bridge gives the host. VMs reach fleetboot here.
BRIDGE_IP_GUEST_VIEW = "192.168.99.1"
DEFAULT_FLEETBOOT_PORT = 8080
TFTP_PORT = 69


def _read_or_generate_secrets() -> dict[str, str]:
    """Persist secrets in build/dev/secrets.env across restarts."""
    DEV_DIR.mkdir(parents=True, exist_ok=True)
    if SECRETS_FILE.is_file():
        env: dict[str, str] = {}
        for line in SECRETS_FILE.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip()
        if "FLEETBOOT_MINT_SECRET" in env and "FLEETBOOT_ADMIN_SECRET" in env:
            return env
    out = {
        "FLEETBOOT_MINT_SECRET": _secrets.token_hex(32),
        "FLEETBOOT_ADMIN_SECRET": _secrets.token_hex(32),
    }
    SECRETS_FILE.write_text("\n".join(f"{k}={v}" for k, v in out.items()) + "\n")
    SECRETS_FILE.chmod(0o600)
    return out


def _detect_lan_ip() -> Optional[str]:
    """Best-effort: open a UDP socket toward an off-link address and read
    back the source IP the kernel chose. No packets actually leave."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("10.255.255.255", 1))
            return sock.getsockname()[0]
    except OSError:
        return None


def _ip_neigh_lookup(ip: str) -> Optional[str]:
    """Resolve IP -> MAC via the kernel's neighbour table.

    Reliable on a shared L2 (the libvirt bridge); returns None when the
    kernel has no entry yet, which leaves the asserted MAC as the sole
    signal — same fallback the qemu functional test uses.
    """
    try:
        result = subprocess.run(
            ["ip", "neigh", "show", ip],
            capture_output=True, text=True, timeout=2,
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        return None
    for line in result.stdout.splitlines():
        parts = line.split()
        for index, part in enumerate(parts):
            if part == "lladdr" and index + 1 < len(parts):
                return parts[index + 1]
    return None


def _permissive_or_real_neighbour(ip: str) -> str:
    """Real ARP if we know, otherwise punt to a sentinel that lets the
    asserted MAC stand alone. In a dev workflow the bridge enforces L2
    integrity well enough."""
    mac = _ip_neigh_lookup(ip)
    # Returning the IP itself as the "MAC" if we don't know it ensures
    # the consistency check sees a deterministic non-match — the test
    # fixture's permissive_neighbour pattern. Real callers should set
    # this up properly.
    return mac or "00:00:00:00:00:00"


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--port", type=int, default=DEFAULT_FLEETBOOT_PORT,
        help="HTTP port for fleetboot (default 8080)",
    )
    parser.add_argument(
        "--host", default="0.0.0.0",
        help="HTTP bind address (default 0.0.0.0)",
    )
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    secrets_env = _read_or_generate_secrets()

    BOOT_DIR.mkdir(parents=True, exist_ok=True)

    # Persistent: shares the registry's DB file so server restarts keep
    # booted machines' tokens alive (in-image heartbeats then keep ticking).
    sessions = BootSessionStore(REGISTRY_PATH)
    registry = MachineRegistry(REGISTRY_PATH)

    app = create_app(
        sessions=sessions,
        mint_secret=secrets_env["FLEETBOOT_MINT_SECRET"],
        admin_secret=secrets_env["FLEETBOOT_ADMIN_SECRET"],
        registry=registry,
        boot_dir=BOOT_DIR,
        dashboard_repo_root=REPO_ROOT,
    )

    # Bring up tftpjail in the same process — the VMs need both.
    fleetboot_url = f"http://{BRIDGE_IP_GUEST_VIEW}:{args.port}"
    client = FleetbootClient(
        base_url=fleetboot_url,
        mint_secret=secrets_env["FLEETBOOT_MINT_SECRET"],
    )
    policy = Policy(
        registry_lookup=build_registry_lookup(client),
        asset_renderer=build_grub_config_renderer(
            fleetboot_client=client,
            fleetboot_base_url=fleetboot_url,
        ),
    )
    tftpjail = TftpJailServer(
        host="0.0.0.0",
        port=TFTP_PORT,
        policy=policy,
        neighbour_lookup=_permissive_or_real_neighbour,
        public_assets_dir=BOOT_DIR,
        ack_timeout_seconds=1.0,
        max_retries=5,
    )
    tftpjail.start()
    print(f"tftpjail listening on UDP/{TFTP_PORT}")
    print(f"fleetboot listening on http://{args.host}:{args.port}")
    print()
    print("dashboard URLs (auth: 'admin' / <admin-secret> below):")
    print(f"  on this host:        http://localhost:{args.port}/dashboard")
    lan_ip = _detect_lan_ip()
    if lan_ip:
        print(
            f"  on the LAN/SSH:      http://{lan_ip}:{args.port}/dashboard"
        )
        print(
            f"  via SSH tunnel:      ssh -L {args.port}:localhost:{args.port} "
            f"{lan_ip}  → http://localhost:{args.port}/dashboard"
        )
    print()
    print(
        f"admin secret: {secrets_env['FLEETBOOT_ADMIN_SECRET']}",
        file=sys.stderr,
    )
    print("press Ctrl-C to stop", file=sys.stderr)
    # Force-flush so the URLs above appear before uvicorn's own banner,
    # even when stdout is captured to a pipe / file by `make` or systemd.
    sys.stdout.flush()
    sys.stderr.flush()

    # Run uvicorn on the foreground thread so Ctrl-C is clean.
    def stop(*_: object) -> None:
        tftpjail.stop()

    signal.signal(signal.SIGINT, lambda *_: (stop(), sys.exit(0)))
    uvicorn.run(
        app, host=args.host, port=args.port,
        log_level="info", access_log=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
