"""Thin HTTP client for the Fleetboot control plane.

Two surfaces, both authenticated with the same shared "mint" secret because
both are tftpjail-facing:

  - ``POST /sessions``    mint a per-boot session token.
  - ``GET  /resolve/<mac>`` read-only registry lookup for a MAC.

Both are isolated here so the rest of tftpjail (server, grub template, policy
seams) stays HTTP-free and easy to test with a fake client.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional, Protocol

import httpx


_DEFAULT_TIMEOUT_SECONDS = 5.0


class _HttpClient(Protocol):
    """Subset of httpx.Client we use — narrowed for easy mocking."""

    def post(
        self, url: str, *, json: dict, headers: dict
    ) -> httpx.Response: ...

    def get(
        self, url: str, *, headers: dict
    ) -> httpx.Response: ...


class MintFailedError(RuntimeError):
    """Raised when fleetboot's /sessions endpoint refuses or errors."""


class ResolveFailedError(RuntimeError):
    """Raised when /resolve returns an unexpected non-200/404 response."""


@dataclass(frozen=True)
class RegisteredMachine:
    """The shape /resolve returns, mirrored on the tftpjail side."""

    mac: str
    profile_name: str
    architecture: str
    platform: str
    # When True, the rendered grub.cfg appends ``console=ttyS0`` to the kernel
    # cmdline. Set on VMs / headless debug hardware; off on real desktops.
    serial_console: bool = False
    # Provenance: 'manual' when an admin entered the row, or 'rule:<name>'
    # when /resolve auto-enrolled this MAC. Forwarded for dashboard display.
    enrolled_by: str = "manual"
    # Local-disk scratch behaviour from the registry — passed to the
    # renderer so the booted image knows what to do with its disk.
    scratch_mode: str = "volatile"


class FleetbootClient:
    """Calls into a running Fleetboot server."""

    def __init__(
        self,
        *,
        base_url: str,
        mint_secret: str,
        http_client: _HttpClient | None = None,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        if not base_url:
            raise ValueError("base_url must not be empty")
        if not mint_secret:
            raise ValueError("mint_secret must not be empty")
        self._base_url = base_url.rstrip("/")
        self._mint_secret = mint_secret
        # An injected client is used as-is (tests, alternative HTTP libs).
        # Otherwise we own a real ``httpx.Client`` with a sensible timeout.
        self._http_client = http_client
        self._timeout_seconds = timeout_seconds

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._mint_secret}"}

    def mint_session(self, mac: str) -> str:
        """Mint a per-boot session token bound to ``mac``. Returns the token."""
        url = f"{self._base_url}/sessions"
        payload = {"mac": mac}
        headers = self._auth_headers()

        if self._http_client is not None:
            response = self._http_client.post(url, json=payload, headers=headers)
        else:
            with httpx.Client(timeout=self._timeout_seconds) as client:
                response = client.post(url, json=payload, headers=headers)

        if response.status_code != 201:
            raise MintFailedError(
                f"fleetboot /sessions returned {response.status_code}: "
                f"{response.text!r}"
            )
        body = response.json()
        token = body.get("token")
        if not token:
            raise MintFailedError("fleetboot /sessions returned no token")
        return token

    def lookup_machine(
        self,
        mac: str,
        source_ip: Optional[str] = None,
        platform: Optional[str] = None,
    ) -> Optional[RegisteredMachine]:
        """Look a MAC up in the fleetboot registry.

        Returns ``None`` if the MAC is not registered AND no auto-enrol rule
        matched (HTTP 404). If a rule does match, the server enrols on the
        spot and returns the new record, transparent to us.

        Raises ``ResolveFailedError`` on any other non-200 status — this
        is a signal that something has gone wrong with the wire, not a
        deny. ``source_ip`` is forwarded so server-side rules can match on
        the client's network location too.
        """
        url = f"{self._base_url}/resolve/{mac}"
        query: list[str] = []
        if source_ip:
            query.append(f"source_ip={source_ip}")
        if platform:
            query.append(f"platform={platform}")
        if query:
            url += "?" + "&".join(query)
        headers = self._auth_headers()

        if self._http_client is not None:
            response = self._http_client.get(url, headers=headers)
        else:
            with httpx.Client(timeout=self._timeout_seconds) as client:
                response = client.get(url, headers=headers)

        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise ResolveFailedError(
                f"fleetboot /resolve returned {response.status_code}: "
                f"{response.text!r}"
            )
        body = response.json()
        return RegisteredMachine(
            mac=body["mac"],
            profile_name=body["profile_name"],
            architecture=body["architecture"],
            platform=body["platform"],
            serial_console=bool(body.get("serial_console", False)),
            enrolled_by=body.get("enrolled_by", "manual"),
            scratch_mode=body.get("scratch_mode", "volatile"),
        )


def build_registry_lookup(
    client: FleetbootClient,
) -> Callable[..., Optional[RegisteredMachine]]:
    """Adapt a FleetbootClient into the ``Policy.registry_lookup`` callable.

    ``Policy`` treats the returned object as opaque "profile" — anything
    truthy means the MAC is known. We hand back the full ``RegisteredMachine``
    so a downstream renderer can see arch/platform without re-asking.

    The optional ``source_ip`` second parameter is threaded through to
    fleetboot's /resolve so server-side auto-enrol rules can match on the
    requester's network location.
    """

    def _lookup(
        mac: str,
        source_ip: Optional[str] = None,
        platform: Optional[str] = None,
    ) -> Optional[RegisteredMachine]:
        return client.lookup_machine(
            mac, source_ip=source_ip, platform=platform,
        )

    return _lookup
