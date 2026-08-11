"""Background reaper for expired compute leases.

A lease (see the reservation system in ``registry``) carries a TTL. When
it passes, the machine must go back to the pool AND be powered off —
otherwise a crashed agent that never called ``release`` would leak a
running machine forever. The reaper closes that gap: it periodically
releases expired leases and powers the freed machines off.

Kept deliberately small and decoupled: it takes the registry and a
``power_off`` callable, so the app wires it to the real power layer while
tests drive ``reap_once`` directly with a fake. The periodic thread is
started explicitly by the production entry point, not by ``create_app``,
so importing/constructing the app in a test spawns no background threads.
"""

from __future__ import annotations

import logging
import threading
from typing import Callable


logger = logging.getLogger(__name__)


class ReservationReaper:
    """Releases expired leases and powers off the freed machines."""

    def __init__(
        self,
        *,
        registry,
        power_off: Callable[[str], None],
        interval_seconds: float = 60.0,
    ) -> None:
        self._registry = registry
        self._power_off = power_off
        self._interval = interval_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def reap_once(self) -> list[str]:
        """Release expired leases, power each freed machine off, return
        the MACs. Powering off is best-effort — the lease is already
        released, so a machine we can't reach is still back in the pool
        (it will be identical when it next boots anyway)."""
        freed = self._registry.reap_expired()
        for mac in freed:
            try:
                self._power_off(mac)
            except Exception:  # noqa: BLE001 — never let one machine stop the sweep
                logger.warning(
                    "reservation reaper: power-off failed for %s", mac,
                    exc_info=True,
                )
        if freed:
            logger.info("reservation reaper: released %d expired lease(s)", len(freed))
        return freed

    def start(self) -> None:
        """Start the periodic sweep in a daemon thread (idempotent)."""
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="reservation-reaper",
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        # wait() returns True when stop() is signalled, False on timeout —
        # so this ticks every interval until asked to stop.
        while not self._stop.wait(self._interval):
            try:
                self.reap_once()
            except Exception:  # noqa: BLE001 — a bad sweep must not kill the loop
                logger.warning("reservation reaper: sweep failed", exc_info=True)
