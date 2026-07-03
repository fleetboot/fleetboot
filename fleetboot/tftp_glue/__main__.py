"""Container entry point for the tftpjail service.

Same shape as `tests/dev/run_server.py`'s tftpjail-half but standalone.
Talks to fleetboot only via HTTP (uses `http_intercept`), so this
process can live in a separate container without shared memory or
a shared database.

Env vars:
  FLEETBOOT_URL              REQUIRED (e.g. http://fleetboot:8080)
  FLEETBOOT_MINT_SECRET      REQUIRED (used to authenticate to
                             fleetboot's /sessions and /resolve APIs)
  PUBLIC_ASSETS_DIR          default /build
  TFTP_HOST                  default 0.0.0.0
  TFTP_PORT                  default 69
  CLIENT_BASE_URL            default = FLEETBOOT_URL. What GRUB is
                             told to talk to for HTTP fetches — often
                             different from the container-internal
                             FLEETBOOT_URL (LAN IP vs. compose DNS).
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import time
from pathlib import Path

from tftpjail.policy import Policy
from tftpjail.server import TftpJailServer

from fleetboot.tftp_glue.client import FleetbootClient, build_registry_lookup
from fleetboot.tftp_glue.http_intercept import make_http_grub_event_intercept
from fleetboot.tftp_glue.renderer import build_grub_config_renderer


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        print(
            f"fleetboot.tftp_glue: {name} must be set",
            file=sys.stderr,
        )
        sys.exit(2)
    return value


def _no_neighbour(_ip: str) -> str | None:
    """Placeholder ARP lookup — the container namespace won't have the
    host's neighbour table anyway. Container deployments should trust
    the RRQ's asserted MAC; sniff-the-wire attackers are stopped at the
    fleetboot registry check, not here.
    """
    return None


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("TFTPJAIL_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    internal_url = _require_env("FLEETBOOT_URL")
    mint_secret = _require_env("FLEETBOOT_MINT_SECRET")
    # The URL the BOOTED MACHINE talks to is usually the host's LAN
    # IP + fleetboot's published port — not the internal
    # container-network name. Fall back to FLEETBOOT_URL if the
    # deployer hasn't told us otherwise.
    client_base_url = os.environ.get("CLIENT_BASE_URL", internal_url)

    public_assets_dir = Path(os.environ.get("PUBLIC_ASSETS_DIR", "/build"))
    if not public_assets_dir.is_dir():
        print(
            f"tftp_glue: public assets dir {public_assets_dir} missing",
            file=sys.stderr,
        )
        sys.exit(2)

    client = FleetbootClient(
        base_url=internal_url, mint_secret=mint_secret,
    )
    policy = Policy(
        registry_lookup=build_registry_lookup(client),
        asset_renderer=build_grub_config_renderer(
            fleetboot_client=client,
            fleetboot_base_url=client_base_url,
        ),
    )

    server = TftpJailServer(
        host=os.environ.get("TFTP_HOST", "0.0.0.0"),
        port=int(os.environ.get("TFTP_PORT", "69")),
        policy=policy,
        neighbour_lookup=_no_neighbour,
        public_assets_dir=public_assets_dir,
        rrq_intercept=make_http_grub_event_intercept(
            fleetboot_base_url=internal_url,
        ),
    )
    server.start()

    stop = False

    def _shutdown(*_: object) -> None:
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    print(
        f"tftpjail listening on UDP/{server.bound_port}, "
        f"fleetboot at {internal_url}",
        flush=True,
    )
    while not stop:
        time.sleep(1.0)
    server.stop()


if __name__ == "__main__":
    main()
