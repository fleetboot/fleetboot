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


def _collect_hardware() -> Optional[dict]:
    """Snapshot CPU/RAM/disks/mounts for the dashboard's machine view.

    Best-effort: each subsection is wrapped in its own try/except, so
    a kernel without one of the /sys/proc layouts (e.g. inside an
    unusual container) doesn't kill the whole inventory.
    """
    import os
    from pathlib import Path

    info: dict = {}

    # CPU model + core count. /proc/cpuinfo lists one block per logical
    # CPU; the `model name` line is the human-readable string.
    try:
        with open("/proc/cpuinfo") as handle:
            for line in handle:
                if line.startswith("model name"):
                    info["cpu_model"] = line.split(":", 1)[1].strip()
                    break
    except OSError:
        pass
    try:
        info["cpu_count"] = os.cpu_count() or 0
    except Exception:
        pass

    # Memory (MemTotal + MemAvailable from /proc/meminfo, in MB).
    try:
        with open("/proc/meminfo") as handle:
            for line in handle:
                if line.startswith("MemTotal:"):
                    info["mem_total_mb"] = int(line.split()[1]) // 1024
                elif line.startswith("MemAvailable:"):
                    info["mem_available_mb"] = int(line.split()[1]) // 1024
    except (OSError, ValueError):
        pass

    # Block devices from /sys/block. Skip loop/ram/sr/dm/zram entries
    # since they're not physical disks.
    disks = []
    try:
        for sysblk in Path("/sys/block").iterdir():
            name = sysblk.name
            if name.startswith(("loop", "ram", "sr", "fd", "dm-", "zram")):
                continue
            try:
                size_sectors = int((sysblk / "size").read_text().strip())
                size_gb = (size_sectors * 512) // (1024 ** 3)
            except (OSError, ValueError):
                continue
            removable = False
            try:
                removable = (sysblk / "removable").read_text().strip() == "1"
            except OSError:
                pass
            model = ""
            try:
                model = (sysblk / "device/model").read_text().strip()
            except OSError:
                pass
            disks.append({
                "name": name,
                "size_gb": size_gb,
                "model": model,
                "removable": removable,
            })
    except OSError:
        pass
    info["disks"] = disks

    # Mount-point free space — only paths that exist; statvfs raises on
    # missing paths. /var/scratch is the canonical fleetboot scratch
    # mountpoint, /home is the autofs root for FreeIPA user homes.
    mounts = []
    for path in ("/", "/var/scratch", "/home"):
        try:
            stat = os.statvfs(path)
        except OSError:
            continue
        total = stat.f_frsize * stat.f_blocks
        if total <= 0:
            continue
        free = stat.f_frsize * stat.f_bavail
        mounts.append({
            "path": path,
            "total_gb": total // (1024 ** 3),
            "free_gb": free // (1024 ** 3),
        })
    info["mounts"] = mounts

    # PCI devices: VGA controllers, network adapters, USB controllers,
    # storage controllers. lspci -mm gives a stable, easy-to-parse
    # quoted format; we then bucket by device class.
    pci_buckets: dict[str, list[str]] = {
        "gpu": [], "network": [], "usb": [], "storage": [],
    }
    raw_pci = _run(["lspci", "-mm"])
    for line in raw_pci.splitlines():
        # Format: "00:02.0" "VGA compatible controller" "Intel..." "Device..." ...
        # We use the class field (column 2) to bucket, and the vendor +
        # device for display.
        import shlex
        try:
            fields = shlex.split(line)
        except ValueError:
            continue
        if len(fields) < 4:
            continue
        klass = fields[1].lower()
        device_str = f"{fields[2]} {fields[3]}"
        if "vga" in klass or "3d" in klass or "display" in klass:
            pci_buckets["gpu"].append(device_str)
        elif "network" in klass or "ethernet" in klass or "wireless" in klass:
            pci_buckets["network"].append(device_str)
        elif "usb" in klass:
            pci_buckets["usb"].append(device_str)
        elif "sata" in klass or "ide" in klass or "raid" in klass or "scsi" in klass:
            pci_buckets["storage"].append(device_str)
    info["pci"] = {k: v for k, v in pci_buckets.items() if v}

    # USB devices currently attached. lsusb output is one device per line:
    # "Bus 001 Device 003: ID 1d6b:0002 Linux Foundation 2.0 root hub".
    # Skip the root hubs (they're noise); the rest is "what's actually
    # plugged into this box".
    usb_devices = []
    raw_usb = _run(["lsusb"])
    for line in raw_usb.splitlines():
        if "root hub" in line.lower():
            continue
        # Take everything after the ID part for a readable label.
        if "ID " in line:
            usb_devices.append(line.split("ID ", 1)[1].strip())
    info["usb_devices"] = usb_devices

    return info if info else None


def _collect_diagnostics() -> Optional[str]:
    """A short snapshot of systemd state useful for "why is boot stuck"
    questions. Server stores the latest on the machine row; the
    machine detail page renders it as a <pre>."""
    sections: list[str] = []
    failed = _run(
        ["systemctl", "list-units", "--failed", "--no-legend", "--no-pager"],
    ).strip()
    sections.append("# systemctl --failed\n" + (failed or "(none)"))

    # Active state of services we care about for typical "stuck"
    # scenarios. is-active prints 'active', 'inactive', 'failed', etc.
    service_states = []
    for svc in (
        "display-manager.service",
        "lightdm.service",
        "ssh.service",
        "fleetboot-scratch-setup.service",
        "fleetboot-set-hostname.service",
    ):
        state = _run(["systemctl", "is-active", svc]).strip() or "unknown"
        service_states.append(f"  {svc}: {state}")
    sections.append("# key service states\n" + "\n".join(service_states))

    # Recent log lines for the scratch setup — if mounts is empty in
    # the hardware inventory, this tells us why.
    scratch_log = _run(
        [
            "journalctl", "-u", "fleetboot-scratch-setup.service",
            "-n", "30", "--no-pager", "--no-hostname",
        ],
        timeout=6.0,
    ).strip()
    if scratch_log:
        sections.append("# scratch-setup journal\n" + scratch_log)

    # Recent kernel messages: useful for "thermal trip", "I/O error",
    # "out of memory", "broken hardware" — none of which the systemd
    # status alone surfaces.
    dmesg_tail = _run(
        ["dmesg", "--time-format=iso", "--ctime", "--no-pager"],
        timeout=6.0,
    ).strip()
    if dmesg_tail:
        last_lines = "\n".join(dmesg_tail.splitlines()[-50:])
        sections.append("# dmesg (last 50 lines)\n" + last_lines)

    # Recent system-wide journal: complements the per-service journals
    # above by catching multi-service interaction issues (e.g. lightdm
    # racing with sssd, NetworkManager spam).
    system_journal = _run(
        ["journalctl", "-n", "50", "--no-pager", "--no-hostname"],
        timeout=6.0,
    ).strip()
    if system_journal:
        sections.append("# journalctl (last 50 lines)\n" + system_journal)

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


def _state_to_string(state: "BootState | str") -> str:
    """Coerce a well-known BootState or a raw custom state to its wire
    representation. Everything downstream — the JSON payload, the
    heartbeat state file, and the boot_events table — deals in
    strings so custom states from profile-specific hooks don't have
    to be added to the BootState enum first."""
    return state.value if isinstance(state, BootState) else state


def _remember_current_state(state: "BootState | str") -> None:
    """Persist the latest reported state for the heartbeat to find.

    Accepts either a BootState (well-known) or a plain string (custom).
    """
    try:
        import os
        os.makedirs("/run/fleetboot", exist_ok=True)
        with open(CURRENT_STATE_PATH, "w", encoding="utf-8") as handle:
            handle.write(_state_to_string(state) + "\n")
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
    state: "BootState | str",
    detail: Optional[str] = None,
    *,
    settings: ReporterSettings | None = None,
    client: httpx.Client | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> None:
    """Send one report. Raises on transport or HTTP errors.

    `state` may be a `BootState` (well-known lifecycle stage) or an
    arbitrary short string (profile-specific custom state).

    `settings` and `client` are injectable so tests can drive this end-to-end
    against the FastAPI app without hitting a real network.
    """
    effective_settings = settings if settings is not None else read_settings()
    payload: dict[str, str] = {"state": _state_to_string(state)}
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
    # Hardware inventory: CPU + RAM + disks + free space. Useful for
    # fleet visibility (which machine has which hardware) and for
    # operational alerts (a disk is filling up).
    hardware = _collect_hardware()
    if hardware:
        payload["hardware"] = hardware
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
    # Soft-reboot signal: the server tells us to reboot via the
    # `pending_reboot` field in the /status reply. Used as a fallback
    # when the dashboard's PDU power-cycle attempt failed (or no PDU
    # is configured). We honour it best-effort — a missing systemd or
    # non-zero return is benign for the current call (the next
    # heartbeat will see the flag still set and try again).
    try:
        body = response.json() or {}
    except Exception:
        body = {}
    if body.get("pending_reboot"):
        try:
            import subprocess
            subprocess.Popen(
                ["systemctl", "reboot"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except (FileNotFoundError, OSError):
            # /sbin/systemctl missing in unusual setups — try the
            # legacy `reboot` binary as a last resort.
            try:
                subprocess.Popen(
                    ["reboot"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
            except (FileNotFoundError, OSError):
                pass


def main(argv: list[str]) -> int:
    """CLI entry point used by systemd units and custom-state hooks.

    The state argument may be a well-known BootState (grub_running,
    network_up, login_console, ...) or any profile-specific custom
    string (e.g. github-runner sends `runner_started`). The server
    ranks known states for `latest_state` and treats unknowns as
    log-only boot events.
    """
    if not argv or len(argv) > 2:
        print(
            "usage: python3 -m fleetboot.reporter.report <state> [detail]",
            file=sys.stderr,
        )
        return 2
    raw_state = argv[0].strip()
    if not raw_state or len(raw_state) > 64 or " " in raw_state:
        print(
            f"reporter: state must be a short single-word identifier, got "
            f"{argv[0]!r}",
            file=sys.stderr,
        )
        return 2
    try:
        state: BootState | str = BootState(raw_state)
    except ValueError:
        state = raw_state
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
