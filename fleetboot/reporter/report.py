"""Posts a single boot-state transition to the Fleetboot server.

Called by the systemd oneshot units and the PAM session hook in the image.
Designed to be invoked as `python3 -m fleetboot.reporter.report <state> [detail]`.
"""

from __future__ import annotations

import socket
import subprocess
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


# The live-boot initramfs hands the kernel a default hostname like
# `debian-live` before DHCP runs. Reporting that has no value, so we
# filter it out and leave the dashboard's hostname column blank instead.
_BORING_HOSTNAMES = frozenset(
    {
        "", "localhost", "(none)", "none",
        "debian", "debian-live",
        # debos's build VM hostname — leaks into /etc/hostname inside the
        # built image unless the recipe overrides it.
        "fakemachine",
        # Our own placeholder set by the recipe; meaningful only after
        # something better (DHCP option 12, etc.) has overwritten it.
        "fleetboot", "fleetboot-client",
    }
)


def _current_hostname() -> Optional[str]:
    """Best-effort hostname read; returns None if it's not meaningful yet."""
    try:
        candidate = socket.gethostname()
    except OSError:
        return None
    if candidate.lower() in _BORING_HOSTNAMES:
        return None
    return candidate


BOOT_VERSION_PATH = "/etc/fleetboot/build-version"


def _run(argv: list[str], timeout: float = 4.0) -> str:
    """Run a subprocess and return its stdout, swallowing errors.

    Diagnostic collection mustn't ever block or fail the reporter — any
    failure returns an empty string and the field just stays empty.
    """
    try:
        result = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return ""
    return result.stdout or ""


# Diagnostics payload is capped well under the server's 8 KB limit. We
# care about WHAT is failing, not exhaustive logs.
DIAGNOSTICS_MAX_BYTES = 6 * 1024


def _collect_diagnostics() -> Optional[str]:
    """A short snapshot of systemd state useful for "why is boot stuck"
    questions. Server stores the latest on the machine row; the
    machine detail page renders it as a <pre>."""
    sections: list[str] = []
    failed = _run(
        ["systemctl", "list-units", "--failed", "--no-legend", "--no-pager"],
    ).strip()
    sections.append("# systemctl --failed\n" + (failed or "(none)"))

    # Display manager state is the #1 thing that gates login_ready.
    dm_state = _run(
        ["systemctl", "is-active", "display-manager.service"],
    ).strip()
    sections.append(f"# display-manager.service: {dm_state or 'unknown'}")

    # Show the most-recent log lines for whatever has just crashed — bounded
    # so a verbose service can't blow the payload budget.
    if failed:
        # First service name on each failed line.
        first_failed = failed.splitlines()[0].split()[0]
        recent_log = _run(
            [
                "journalctl", "-u", first_failed,
                "-n", "30", "--no-pager", "--no-hostname",
            ],
            timeout=6.0,
        ).strip()
        if recent_log:
            sections.append(
                f"# recent journal for {first_failed}\n{recent_log}"
            )

    body = "\n\n".join(s for s in sections if s)
    if not body:
        return None
    if len(body) > DIAGNOSTICS_MAX_BYTES:
        # Truncate cleanly — keep the start (header + earliest log).
        body = body[: DIAGNOSTICS_MAX_BYTES - 32] + "\n…(truncated)"
    return body

# Where the reporter records "the state we most recently told the server".
# The heartbeat timer reads this and re-sends so the dashboard stays alive.
CURRENT_STATE_PATH = "/run/fleetboot/current-state"


def _remember_current_state(state: BootState) -> None:
    """Persist the latest reported state for the heartbeat to find."""
    try:
        import os
        os.makedirs("/run/fleetboot", exist_ok=True)
        with open(CURRENT_STATE_PATH, "w", encoding="utf-8") as handle:
            handle.write(state.value + "\n")
    except OSError:
        pass


def _read_boot_version() -> Optional[str]:
    """Read the image build version stamp written by `make image`.

    The file is one line; everything after the first newline is ignored
    so future format extensions (signature, profile name, etc.) don't
    break older reporters.
    """
    try:
        with open(BOOT_VERSION_PATH, "r", encoding="utf-8") as handle:
            line = handle.readline().strip()
    except OSError:
        return None
    return line or None


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
    # Include the kernel-visible hostname so the dashboard can show a
    # human-friendly name. DHCP/DNS substitutes the right value in
    # /etc/hostname during early boot; we just read what's settled.
    hostname = _current_hostname()
    if hostname:
        payload["hostname"] = hostname
    # Include the image build version so the dashboard can tell whether
    # this machine is running the most recently published squashfs.
    boot_version = _read_boot_version()
    if boot_version:
        payload["boot_version"] = boot_version
    # And a snapshot of what's broken — best-effort, so a hung
    # systemctl can't wedge the reporter.
    diagnostics = _collect_diagnostics()
    if diagnostics:
        payload["diagnostics"] = diagnostics
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
    # Remember the most-recent successfully-reported state so the heartbeat
    # timer can re-send it. Best-effort: a missing /run/fleetboot dir or a
    # read-only fs just leaves the heartbeat with nothing to send, which is
    # also benign.
    _remember_current_state(state)


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
