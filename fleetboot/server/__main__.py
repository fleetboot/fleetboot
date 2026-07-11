"""Production entry point for the fleetboot control plane.

Reads config from env vars, wires the app, runs uvicorn. Started by
`docker compose up fleetboot` — see docker-compose.yml.

Env vars:
  FLEETBOOT_HOST                default 0.0.0.0
  FLEETBOOT_PORT                default 8080
  FLEETBOOT_DB                  default /data/machines.sqlite
  FLEETBOOT_BOOT_DIR            default /build
  FLEETBOOT_PROFILES_ROOT       default /app/image/profiles
  FLEETBOOT_REPO_ROOT           default /app (drives make image / build UI)
  FLEETBOOT_ADMIN_SECRET        REQUIRED for dashboard access
  FLEETBOOT_MINT_SECRET         REQUIRED for tftpjail /sessions endpoint
  FLEETBOOT_AUTHORIZED_KEYS     optional path to a keys file to serve
  FLEETBOOT_CLIENT_BASE_URL     optional; baked into grub.cfg. If unset,
                                clients use whatever URL they resolved via
                                DHCP / hostname — for docker deployments
                                on the same LAN this is normally the host
                                machine's LAN IP + FLEETBOOT_PORT.
  FLEETBOOT_TLS_CERT            optional path to a PEM cert. When set
                                (together with FLEETBOOT_TLS_KEY), uvicorn
                                serves HTTPS instead of HTTP. Admin owns
                                the cert source (ACME, an internal CA, a
                                FreeIPA-issued cert, self-signed) — this
                                process just consumes the files.
  FLEETBOOT_TLS_KEY             optional path to the matching PEM key.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import uvicorn

from fleetboot.server.app import create_app
from fleetboot.server.boot_sessions import BootSessionStore
from fleetboot.server.registry import MachineRegistry


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        print(
            f"fleetboot.server: {name} must be set",
            file=sys.stderr,
        )
        sys.exit(2)
    return value


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("FLEETBOOT_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    db_path = Path(os.environ.get("FLEETBOOT_DB", "/data/machines.sqlite"))
    db_path.parent.mkdir(parents=True, exist_ok=True)

    boot_dir = Path(os.environ.get("FLEETBOOT_BOOT_DIR", "/build"))
    repo_root = Path(os.environ.get("FLEETBOOT_REPO_ROOT", "/app"))
    profiles_root = Path(
        os.environ.get(
            "FLEETBOOT_PROFILES_ROOT",
            str(repo_root / "image" / "profiles"),
        ),
    )

    admin_secret = _require_env("FLEETBOOT_ADMIN_SECRET")
    mint_secret = _require_env("FLEETBOOT_MINT_SECRET")

    authorized_keys_env = os.environ.get("FLEETBOOT_AUTHORIZED_KEYS")
    authorized_keys_path = (
        Path(authorized_keys_env) if authorized_keys_env else None
    )

    sessions = BootSessionStore(db_path)
    registry = MachineRegistry(db_path)

    app = create_app(
        sessions=sessions,
        registry=registry,
        admin_secret=admin_secret,
        mint_secret=mint_secret,
        boot_dir=boot_dir,
        dashboard_repo_root=repo_root,
        authorized_keys_path=authorized_keys_path,
    )

    kwargs: dict = {
        "host": os.environ.get("FLEETBOOT_HOST", "0.0.0.0"),
        "port": int(os.environ.get("FLEETBOOT_PORT", "8080")),
        "log_level": os.environ.get("FLEETBOOT_LOG_LEVEL", "info").lower(),
    }
    # Opt-in HTTPS. Both cert and key must be present — one without
    # the other is almost certainly a misconfiguration, so fail
    # loudly at startup rather than silently falling back to HTTP.
    tls_cert = os.environ.get("FLEETBOOT_TLS_CERT")
    tls_key = os.environ.get("FLEETBOOT_TLS_KEY")
    if tls_cert or tls_key:
        if not (tls_cert and tls_key):
            print(
                "fleetboot.server: FLEETBOOT_TLS_CERT and "
                "FLEETBOOT_TLS_KEY must be set together",
                file=sys.stderr,
            )
            sys.exit(2)
        if not Path(tls_cert).is_file() or not Path(tls_key).is_file():
            print(
                "fleetboot.server: FLEETBOOT_TLS_CERT / _KEY "
                "point at files that don't exist",
                file=sys.stderr,
            )
            sys.exit(2)
        kwargs["ssl_certfile"] = tls_cert
        kwargs["ssl_keyfile"] = tls_key

    uvicorn.run(app, **kwargs)


if __name__ == "__main__":
    main()
