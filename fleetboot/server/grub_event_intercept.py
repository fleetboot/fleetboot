"""TFTP-routed grub-event handler.

GRUB on the OptiPlex's BIOS PXE TCP stack adds ~34s per HTTP `cat` call —
likely FIN-propagation timing on the old NIC's UNDI driver. UDP TFTP
through the same UNDI path serves kernels in milliseconds, so we route
the grub-stage events over TFTP instead and let tftpjail forward them
to us in-process.

The renderer emits per-MAC grub.cfg lines like:

    source (tftp,${pxe_default_server})/grub-event/<token>/<state>

When tftpjail receives the RRQ, its `rrq_intercept` callback (registered
by `tests.dev.run_server`) calls into here. We:

  1. parse the path,
  2. validate the token via the in-memory BootSessionStore,
  3. validate + record the state via the same in-memory record_state and
     persistent boot_events row that the HTTP /grub-event endpoint uses,
  4. return empty bytes so `source` parses a no-op script and continues.

The HTTP /grub-event endpoint stays in place: tftpjail intercept is the
fast path for BIOS PXE clients; UEFI clients with a less-broken TCP
stack can still use HTTP if a renderer chooses to.
"""

from __future__ import annotations

from typing import Callable, Optional

from fleetboot.boot_states import BootState
from fleetboot.server.boot_sessions import (
    BootSessionStore,
    OutOfOrderStateError,
    UnknownTokenError,
)
from fleetboot.server.registry import MachineRegistry


def make_grub_event_intercept(
    *,
    sessions: BootSessionStore,
    registry: Optional[MachineRegistry] = None,
) -> Callable[[str, tuple[str, int]], Optional[bytes]]:
    """Return an `rrq_intercept` callback for tftpjail.

    Matches paths shaped exactly ``/grub-event/<token>/<state>`` (3
    segments after stripping leading slash). Anything else returns
    ``None`` so the request falls through to tftpjail's normal dispatch.
    """

    def intercept(
        request_filename: str, _client_addr: tuple[str, int],
    ) -> Optional[bytes]:
        parts = request_filename.lstrip("/").split("/")
        if len(parts) != 3 or parts[0] != "grub-event":
            return None
        _, token, state_str = parts

        try:
            state = BootState(state_str)
        except ValueError:
            # Unknown state. Returning empty bytes (rather than None) so
            # the client doesn't get a TFTP error packet — the renderer
            # owns the URL contract, an unknown state means the renderer
            # is out of sync with the server; better to silently no-op
            # than to break the boot.
            return b""

        try:
            session = sessions.record_state(token, state)
        except (UnknownTokenError, OutOfOrderStateError):
            # Bad token / out-of-order: same reasoning as above — don't
            # break the boot for a logging miss.
            return b""

        if registry is not None:
            registry.log_boot_event(
                mac=session.mac, state=state.value, detail=None,
            )
        # Empty body: GRUB's `source` parses zero bytes as a no-op
        # script and falls through to the next line of the per-MAC
        # config.
        return b""

    return intercept
