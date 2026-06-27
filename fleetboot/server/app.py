"""FastAPI app: receives boot-state reports, mints sessions, and serves the
boot assets (kernel, initrd, squashfs) to GRUB and live-boot.

Three endpoint groups, each with a different threat model:

  POST /status        — image-side reporter posts lifecycle state.
                        Auth: per-boot session token (Bearer).
  POST /sessions      — tftpjail mints a per-boot session token to stamp
                        into the rendered grub.cfg.
                        Auth: shared secret (Bearer) — only tftpjail.
  GET  /boot/<file>   — GRUB / live-boot fetches a kernel, initrd, or
                        squashfs.
                        Auth: per-boot session token in `?t=` query, since
                        bootloaders generally cannot set headers.

For unknown tokens, malformed input, or out-of-order states we return uniform
error responses — the boot network is hostile by default, so we do not leak
which-token-exists information to probers.
"""

from __future__ import annotations

import hmac
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Response, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from fleetboot.boot_states import BootState
from fleetboot.server.boot_sessions import (
    BootSessionStore,
    OutOfOrderStateError,
    UnknownTokenError,
)
from fleetboot.server.registry import Machine, MachineRegistry


# Closed allowlist of files we will serve under /boot/. Anything not in this
# set is rejected before any filesystem lookup — no path traversal, no
# directory listing, no oracle for what else is on disk.
ALLOWED_BOOT_FILES = frozenset(
    {
        "vmlinuz",
        "initrd.img",
        "fleetboot-amd64.squashfs",
        "fleetboot-arm64.squashfs",
    }
)


class StatusReport(BaseModel):
    """The payload a machine sends to /status."""

    state: BootState = Field(
        ...,
        description="The lifecycle state the machine has just entered.",
    )
    # Optional details — the user_logged_in trigger passes the username here.
    # We never trust this for authorisation, only display.
    detail: Optional[str] = Field(
        default=None, max_length=256, description="Optional human-readable detail."
    )


class StatusAcknowledgement(BaseModel):
    """What the server returns on a successful report."""

    ok: bool = True
    mac: str
    state: BootState


class MintRequest(BaseModel):
    """tftpjail's request to mint a per-boot session token."""

    mac: str = Field(..., description="MAC address the token should bind to.")


class MintResponse(BaseModel):
    """What the server returns on a successful mint."""

    token: str
    mac: str


class MachineEnrolment(BaseModel):
    """Body of POST /machines — an admin registering a fleet machine."""

    mac: str = Field(..., description="MAC address to register.")
    profile_name: str = Field(
        ..., description="Logical profile (image+policy) the machine belongs to."
    )
    architecture: str = Field(
        ..., description="CPU architecture: x86_64, arm64, or i386."
    )
    platform: str = Field(..., description="Firmware platform: efi or pc.")
    # Off by default — real student desktops do not have or need a serial.
    # Tests and headless lab boxes opt in.
    serial_console: bool = Field(
        default=False,
        description=(
            "If true, the renderer adds console=ttyS0 to the kernel cmdline. "
            "Enable for VMs and headless hardware; leave off for desktops."
        ),
    )


class MachineRecord(BaseModel):
    """One machine row as returned by /machines."""

    mac: str
    profile_name: str
    architecture: str
    platform: str
    serial_console: bool
    created_at: str

    @classmethod
    def from_machine(cls, machine: Machine) -> "MachineRecord":
        return cls(
            mac=machine.mac,
            profile_name=machine.profile_name,
            architecture=machine.architecture,
            platform=machine.platform,
            serial_console=machine.serial_console,
            created_at=machine.created_at,
        )


def create_app(
    sessions: BootSessionStore | None = None,
    *,
    mint_secret: str | None = None,
    boot_dir: Path | None = None,
    registry: MachineRegistry | None = None,
    admin_secret: str | None = None,
) -> FastAPI:
    """Build the FastAPI app.

    `sessions` — inject an existing store. A fresh in-memory store is
        created if omitted.
    `mint_secret` — shared secret required on /sessions. If None, /sessions
        returns 503 (minting disabled).
    `boot_dir` — directory holding the build artifacts served by /boot/.
        If None, /boot/* returns 503 (boot serving disabled).
    `registry` — MachineRegistry instance. If None, /machines returns 503.
    `admin_secret` — shared secret required on /machines. If None, /machines
        returns 503 even if a registry is configured.
    """
    store = sessions if sessions is not None else BootSessionStore()
    app = FastAPI(title="Fleetboot control plane")

    def get_store() -> BootSessionStore:
        return store

    # Expose runtime config on the app so tests can reach it without going
    # through dependency injection.
    app.state.sessions = store
    app.state.mint_secret = mint_secret
    app.state.boot_dir = boot_dir
    app.state.registry = registry
    app.state.admin_secret = admin_secret

    @app.post("/status", response_model=StatusAcknowledgement)
    def post_status(
        report: StatusReport,
        authorization: str | None = Header(default=None),
        store: BootSessionStore = Depends(get_store),
    ) -> StatusAcknowledgement:
        token = _extract_bearer_token(authorization)
        try:
            session = store.record_state(token, report.state)
        except UnknownTokenError:
            # Uniform 401: do not distinguish "unknown token" from "missing".
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="unauthorised",
            )
        except OutOfOrderStateError as err:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=str(err),
            )
        return StatusAcknowledgement(
            ok=True, mac=session.mac, state=report.state
        )

    @app.post(
        "/sessions",
        response_model=MintResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def mint_session(
        request: MintRequest,
        authorization: str | None = Header(default=None),
        store: BootSessionStore = Depends(get_store),
    ) -> MintResponse:
        if mint_secret is None:
            # No secret configured -> minting is administratively disabled.
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="minting not configured",
            )
        presented = _extract_bearer_token(authorization)
        # Constant-time comparison so timing does not leak the secret length.
        if not presented or not hmac.compare_digest(presented, mint_secret):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="unauthorised",
            )
        session = store.mint(request.mac)
        return MintResponse(token=session.token, mac=session.mac)

    @app.get("/boot/{token}/{filename}")
    def serve_boot_file(
        token: str,
        filename: str,
        store: BootSessionStore = Depends(get_store),
    ) -> FileResponse:
        """Token in the path (not the query string) so live-boot's URL parser
        sees the real file extension. live-boot's mount-http.sh determines
        the archive type by ``sed 's/.*\\.\\(.*\\)/\\1/'`` on the URL — a
        query string like ``?t=...`` would put the token AFTER the dot and
        the file would be unrecognised."""
        if boot_dir is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="boot serving not configured",
            )
        # Filename allowlist before any filesystem operation. Same wire-level
        # response for "unknown name" and "missing on disk" so probers cannot
        # enumerate what we have.
        if filename not in ALLOWED_BOOT_FILES:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="not found"
            )
        if store.lookup(token) is None:
            # Uniform 401: same as /status.
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="unauthorised",
            )
        path = boot_dir / filename
        if not path.is_file():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="not found"
            )
        return FileResponse(str(path), media_type="application/octet-stream")

    # ---- /machines admin API ---------------------------------------------

    def _require_admin(authorization: str | None) -> None:
        """Reject anything that isn't the admin shared secret."""
        if registry is None or admin_secret is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="registry not configured",
            )
        presented = _extract_bearer_token(authorization)
        if not presented or not hmac.compare_digest(presented, admin_secret):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="unauthorised",
            )

    @app.post(
        "/machines",
        response_model=MachineRecord,
        status_code=status.HTTP_201_CREATED,
    )
    def enroll_machine(
        body: MachineEnrolment,
        authorization: str | None = Header(default=None),
    ) -> MachineRecord:
        _require_admin(authorization)
        # registry is non-None: _require_admin only returns successfully when
        # it has been configured.
        machine = registry.enroll(  # type: ignore[union-attr]
            mac=body.mac,
            profile_name=body.profile_name,
            architecture=body.architecture,
            platform=body.platform,
            serial_console=body.serial_console,
        )
        return MachineRecord.from_machine(machine)

    @app.get("/machines", response_model=list[MachineRecord])
    def list_machines(
        authorization: str | None = Header(default=None),
    ) -> list[MachineRecord]:
        _require_admin(authorization)
        return [
            MachineRecord.from_machine(m)
            for m in registry.list_all()  # type: ignore[union-attr]
        ]

    @app.get("/machines/{mac}", response_model=MachineRecord)
    def get_machine(
        mac: str, authorization: str | None = Header(default=None),
    ) -> MachineRecord:
        _require_admin(authorization)
        machine = registry.lookup(mac)  # type: ignore[union-attr]
        if machine is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="not found"
            )
        return MachineRecord.from_machine(machine)

    @app.get("/resolve/{mac}", response_model=MachineRecord)
    def resolve_machine(
        mac: str, authorization: str | None = Header(default=None),
    ) -> MachineRecord:
        """Read-only registry lookup, authenticated with the mint secret.

        tftpjail uses this on every read-request to decide whether a MAC is
        known and which profile/arch it belongs to. We deliberately give
        tftpjail less than the full admin surface — it only reads.
        """
        if registry is None or mint_secret is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="resolve not configured",
            )
        presented = _extract_bearer_token(authorization)
        if not presented or not hmac.compare_digest(presented, mint_secret):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="unauthorised",
            )
        machine = registry.lookup(mac)
        if machine is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="not found"
            )
        return MachineRecord.from_machine(machine)

    @app.delete("/machines/{mac}")
    def delete_machine(
        mac: str, authorization: str | None = Header(default=None),
    ) -> Response:
        _require_admin(authorization)
        removed = registry.remove(mac)  # type: ignore[union-attr]
        if not removed:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="not found"
            )
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return app


def _extract_bearer_token(header_value: str | None) -> str:
    """Pull the token out of an 'Authorization: Bearer <token>' header.

    Returns an empty string when missing or malformed; the lookup will then
    fail uniformly as 'unknown'.
    """
    if not header_value:
        return ""
    parts = header_value.strip().split(maxsplit=1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return ""
    return parts[1].strip()
