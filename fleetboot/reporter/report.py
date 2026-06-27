"""Posts a single boot-state transition to the Fleetboot server.

Called by the systemd oneshot units and the PAM session hook in the image.
Designed to be invoked as `python3 -m fleetboot.reporter.report <state> [detail]`.
"""

from __future__ import annotations

import sys
from typing import Optional
from urllib.parse import urljoin

import httpx

from fleetboot.boot_states import BootState
from fleetboot.reporter.cmdline import (
    MissingReporterSettingsError,
    ReporterSettings,
    read_settings,
)


# Short, because a boot is in progress and we should not wedge it on a slow
# server. The reporter is best-effort telemetry, not a critical path.
DEFAULT_TIMEOUT_SECONDS = 5.0

STATUS_PATH = "/status"


class ReportFailedError(RuntimeError):
    """Raised when the server returns a non-success status."""


def report_state(
    state: BootState,
    detail: Optional[str] = None,
    *,
    settings: ReporterSettings | None = None,
    client: httpx.Client | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> None:
    """Send one report. Raises on transport or HTTP errors.

    `settings` and `client` are injectable so tests can drive this end-to-end
    against the FastAPI app without hitting a real network.
    """
    effective_settings = settings if settings is not None else read_settings()
    payload: dict[str, str] = {"state": state.value}
    if detail is not None:
        payload["detail"] = detail
    url = urljoin(effective_settings.server_url, STATUS_PATH)
    headers = {"Authorization": f"Bearer {effective_settings.boot_token}"}

    if client is None:
        # Default real-world transport. https in production; tftpjail injects
        # an https URL onto the kernel cmdline.
        with httpx.Client(timeout=timeout) as owned_client:
            response = owned_client.post(url, json=payload, headers=headers)
    else:
        response = client.post(url, json=payload, headers=headers)
    if response.status_code >= 400:
        raise ReportFailedError(
            f"server returned {response.status_code}: {response.text}"
        )


def main(argv: list[str]) -> int:
    """CLI entry point used by systemd units and the PAM hook."""
    if not argv or len(argv) > 2:
        print(
            "usage: python3 -m fleetboot.reporter.report <state> [detail]",
            file=sys.stderr,
        )
        return 2
    try:
        state = BootState(argv[0])
    except ValueError:
        print(f"unknown state: {argv[0]!r}", file=sys.stderr)
        return 2
    detail = argv[1] if len(argv) == 2 else None
    try:
        report_state(state, detail)
    except MissingReporterSettingsError as err:
        print(f"reporter not configured: {err}", file=sys.stderr)
        return 1
    except (ReportFailedError, httpx.HTTPError) as err:
        print(f"report failed: {err}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - thin CLI shim
    raise SystemExit(main(sys.argv[1:]))
