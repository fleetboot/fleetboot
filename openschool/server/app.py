"""FastAPI app: receives boot-state reports from machines on the fleet.

The image-side reporter POSTs to `/status` with a per-boot token in the
Authorization header. We validate the token, record the state, and return a
small JSON acknowledgement.

For unknown tokens, malformed input, or out-of-order states we return uniform
error responses — the boot network is hostile by default, so we do not leak
which-token-exists information to probers.
"""

from __future__ import annotations

from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, status
from pydantic import BaseModel, Field

from openschool.boot_states import BootState
from openschool.server.boot_sessions import (
    BootSessionStore,
    OutOfOrderStateError,
    UnknownTokenError,
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


def create_app(sessions: BootSessionStore | None = None) -> FastAPI:
    """Build the FastAPI app, optionally with an injected session store.

    A fresh store is created by default. Tests can inject their own to assert
    against it, and production wires in a long-lived store.
    """
    store = sessions if sessions is not None else BootSessionStore()
    app = FastAPI(title="OpenSchool status receiver")

    def get_store() -> BootSessionStore:
        return store

    # Expose the store on the app so callers (including tests) can reach it
    # without going through the dependency injection plumbing.
    app.state.sessions = store

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
