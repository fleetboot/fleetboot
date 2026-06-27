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

from fastapi import Depends, FastAPI, Header, HTTPException, Query, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from openschool.boot_states import BootState
from openschool.server.boot_sessions import (
    BootSessionStore,
    OutOfOrderStateError,
    UnknownTokenError,
)


# Closed allowlist of files we will serve under /boot/. Anything not in this
# set is rejected before any filesystem lookup — no path traversal, no
# directory listing, no oracle for what else is on disk.
ALLOWED_BOOT_FILES = frozenset(
    {
        "vmlinuz",
        "initrd.img",
        "openschool-amd64.squashfs",
        "openschool-arm64.squashfs",
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


def create_app(
    sessions: BootSessionStore | None = None,
    *,
    mint_secret: str | None = None,
    boot_dir: Path | None = None,
) -> FastAPI:
    """Build the FastAPI app.

    `sessions` — inject an existing store (tests, multi-app deployments). A
        fresh store is created if omitted.
    `mint_secret` — shared secret required on /sessions. If None, /sessions
        returns 503 (minting disabled). Production reads this from the
        environment and passes it in.
    `boot_dir` — directory holding the build artifacts served by /boot/.
        If None, /boot/* returns 503 (boot serving disabled).
    """
    store = sessions if sessions is not None else BootSessionStore()
    app = FastAPI(title="OpenSchool control plane")

    def get_store() -> BootSessionStore:
        return store

    # Expose runtime config on the app so tests can reach it without going
    # through dependency injection.
    app.state.sessions = store
    app.state.mint_secret = mint_secret
    app.state.boot_dir = boot_dir

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

    @app.get("/boot/{filename}")
    def serve_boot_file(
        filename: str,
        t: str = Query(..., description="Per-boot session token."),
        store: BootSessionStore = Depends(get_store),
    ) -> FileResponse:
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
        if store.lookup(t) is None:
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
