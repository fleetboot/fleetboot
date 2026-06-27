"""Reads the per-boot reporter settings from the kernel command line.

tftpjail injects these when it renders the machine's grub.cfg:

    openschool.server=https://openschool.example/  openschool.boot_token=<hex>

We parse them with a small dedicated function so it can be unit-tested without
touching /proc.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


# Standard location on Linux. Tests pass a different path to read_settings().
DEFAULT_CMDLINE_PATH = Path("/proc/cmdline")

# Key names we look for. Kept as constants so the image-side units, the
# grub.cfg renderer in tftpjail, and this module all agree.
SERVER_KEY = "openschool.server"
TOKEN_KEY = "openschool.boot_token"


@dataclass(frozen=True)
class ReporterSettings:
    """The values needed for a single status POST."""

    server_url: str
    boot_token: str


class MissingReporterSettingsError(RuntimeError):
    """Raised when the kernel command line lacks the required keys.

    Treated as a hard error so the image-side units fail loudly rather than
    silently dropping reports.
    """


def parse_cmdline(cmdline: str) -> ReporterSettings:
    """Pull the reporter settings out of a kernel command-line string."""
    tokens = cmdline.strip().split()
    found: dict[str, str] = {}
    for token in tokens:
        if "=" not in token:
            continue
        key, value = token.split("=", 1)
        if key in (SERVER_KEY, TOKEN_KEY):
            found[key] = value
    if SERVER_KEY not in found or TOKEN_KEY not in found:
        missing = [k for k in (SERVER_KEY, TOKEN_KEY) if k not in found]
        raise MissingReporterSettingsError(
            f"missing required kernel cmdline keys: {', '.join(missing)}"
        )
    return ReporterSettings(
        server_url=found[SERVER_KEY], boot_token=found[TOKEN_KEY]
    )


def read_settings(cmdline_path: Path = DEFAULT_CMDLINE_PATH) -> ReporterSettings:
    """Read and parse the kernel command line."""
    return parse_cmdline(cmdline_path.read_text())
