"""Per-boot session tokens.

tftpjail mints one of these when it renders a machine's grub.cfg and injects it
into the kernel command line. The image's reporter reads it from /proc/cmdline
and presents it on every status POST. The server validates it here.

A token authenticates *this particular boot* of a known MAC — not a user, and
not a long-lived machine identity. It expires when the boot ends. This is
telemetry-grade authentication: it stops spoofed status reports, but it does
not grant any privilege at the OS layer.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from threading import Lock

from fleetboot.boot_states import BootState, state_index


# How many bytes of entropy in a token. 32 bytes -> 256 bits, hex-encoded to 64
# characters. Plenty for an unguessable per-boot secret on a LAN.
TOKEN_ENTROPY_BYTES = 32


@dataclass
class BootSession:
    """One boot of one machine. Created by mint(), consumed by validate()."""

    token: str
    mac: str
    # The highest-ordered state we have seen reported on this session. None
    # means "no report yet". Used to reject out-of-order reports.
    latest_state: BootState | None = None
    # All reports received, in arrival order. Useful for fleet visibility.
    reports: list[BootState] = field(default_factory=list)


class BootSessionStore:
    """Thread-safe in-memory store of active boot sessions.

    In production this would be backed by a database keyed by token, but the
    interface is small enough that swapping the backend later is a single-file
    change. We deliberately keep persistence out of the first slice.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, BootSession] = {}
        self._lock = Lock()

    def mint(self, mac: str) -> BootSession:
        """Mint a fresh session token for a machine that is about to boot.

        Called by tftpjail at the moment it renders a grub.cfg for `mac`.
        """
        normalised_mac = _normalise_mac(mac)
        token = secrets.token_hex(TOKEN_ENTROPY_BYTES)
        session = BootSession(token=token, mac=normalised_mac)
        with self._lock:
            self._sessions[token] = session
        return session

    def lookup(self, token: str) -> BootSession | None:
        """Return the session for a token, or None if unknown."""
        with self._lock:
            return self._sessions.get(token)

    def record_state(self, token: str, state: BootState) -> BootSession:
        """Record a reported state against a session.

        Raises UnknownTokenError if the token is not active, and
        OutOfOrderStateError if the report goes backwards. We accept the same
        state twice as a no-op (units restarting is benign).
        """
        with self._lock:
            session = self._sessions.get(token)
            if session is None:
                raise UnknownTokenError(token)
            if session.latest_state is not None:
                if state_index(state) < state_index(session.latest_state):
                    raise OutOfOrderStateError(
                        previous=session.latest_state, attempted=state
                    )
            session.reports.append(state)
            # Latest reflects the furthest-along state ever seen, even if the
            # caller re-reports an earlier state of equal index.
            if session.latest_state is None or state_index(state) > state_index(
                session.latest_state
            ):
                session.latest_state = state
            return session

    def end(self, token: str) -> None:
        """Drop a session — called when a machine reboots or is taken offline."""
        with self._lock:
            self._sessions.pop(token, None)

    def active_sessions(self) -> list[BootSession]:
        """Snapshot of all currently active sessions (for fleet views/tests)."""
        with self._lock:
            return list(self._sessions.values())


class UnknownTokenError(Exception):
    """Raised when a status report presents a token we do not know."""

    def __init__(self, token: str) -> None:
        super().__init__("unknown boot session token")
        self.token = token


class OutOfOrderStateError(Exception):
    """Raised when a status report goes backwards in the lifecycle order."""

    def __init__(self, previous: BootState, attempted: BootState) -> None:
        super().__init__(
            f"cannot report {attempted.value} after {previous.value}"
        )
        self.previous = previous
        self.attempted = attempted


def _normalise_mac(mac: str) -> str:
    """Lowercase and colon-separate a MAC for consistent storage."""
    cleaned = mac.replace("-", ":").replace(".", ":").lower().strip()
    return cleaned
