"""HTTP-based grub-event intercept for tftpjail.

Used by the tftpjail container which runs as a separate process from
fleetboot. When tftpjail receives a `/grub-event/<token>/<state>`
TFTP RRQ, this callback forwards the request to fleetboot's
`GET /grub-event/<token>/<state>` HTTP endpoint (which does all the
state recording), then returns empty bytes to tftpjail's transfer
loop so GRUB's `source` gets a valid empty response.

Contrast with `fleetboot.server.grub_event_intercept` which does
the same job in-process for the single-process dev harness — no
HTTP hop needed there because tftpjail and fleetboot share memory.
This variant is what any deployment with tftpjail in its own
container / process uses.
"""

from __future__ import annotations

from typing import Callable, Optional

import httpx


def make_http_grub_event_intercept(
    *,
    fleetboot_base_url: str,
    timeout: float = 5.0,
) -> Callable[[str, tuple[str, int]], Optional[bytes]]:
    """Return an rrq_intercept callback that forwards to fleetboot.

    On a matching TFTP RRQ (`/grub-event/<token>/<state>`), fire a
    fire-and-forget-ish HTTP GET at fleetboot. Any HTTP failure is
    swallowed: a missed state report is a nice-to-have, and we
    don't want to fail the RRQ (which would break the boot) just
    because fleetboot briefly hiccupped.

    Non-matching paths return None so tftpjail's normal dispatch
    (public-assets, jail policy) handles them.
    """

    base = fleetboot_base_url.rstrip("/")

    def intercept(
        request_filename: str, _client_addr: tuple[str, int],
    ) -> Optional[bytes]:
        parts = request_filename.lstrip("/").split("/")
        if len(parts) != 3 or parts[0] != "grub-event":
            return None
        _, token, state = parts
        url = f"{base}/grub-event/{token}/{state}"
        try:
            with httpx.Client(timeout=timeout) as client:
                client.get(url)
        except httpx.HTTPError:
            # Best-effort — don't break the boot on a transient
            # fleetboot outage. The in-image reporter's heartbeat
            # will still eventually converge on the right state.
            pass
        # Empty body: GRUB's `source` parses zero bytes as a no-op
        # script and continues to the next line of the per-MAC config.
        return b""

    return intercept
