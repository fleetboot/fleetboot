"""Structural tests for the periodic-heartbeat runtime files in the image."""

from __future__ import annotations

import configparser
import stat
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SYSTEMD_DIR = REPO_ROOT / "image" / "systemd"
RUNTIME_DIR = REPO_ROOT / "image" / "runtime"


def test_heartbeat_script_exists_and_is_executable():
    script = RUNTIME_DIR / "fleetboot-heartbeat.sh"
    assert script.is_file()
    mode = script.stat().st_mode
    assert mode & stat.S_IXUSR


def test_heartbeat_script_reads_current_state_file():
    text = (RUNTIME_DIR / "fleetboot-heartbeat.sh").read_text()
    # The script must consume the file the reporter writes on each
    # successful state report — otherwise it'd have nothing to send.
    assert "/run/fleetboot/current-state" in text
    # And it must invoke the reporter to re-send. Without this line the
    # whole timer is a no-op.
    assert "fleetboot.reporter.report" in text


def test_heartbeat_service_is_oneshot_conditional():
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    parser.read(SYSTEMD_DIR / "fleetboot-heartbeat.service")
    assert parser["Service"]["Type"] == "oneshot"
    # Both conditions matter: skip if the cmdline doesn't have a token
    # (we're not a fleetboot machine), and skip if no state was ever
    # reported (we haven't reached network_up yet).
    text = (SYSTEMD_DIR / "fleetboot-heartbeat.service").read_text()
    assert "ConditionKernelCommandLine=fleetboot.boot_token" in text
    assert "ConditionPathExists=/run/fleetboot/current-state" in text


def test_heartbeat_timer_fires_periodically():
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    parser.read(SYSTEMD_DIR / "fleetboot-heartbeat.timer")
    assert "OnBootSec" in parser["Timer"]
    # `OnUnitActiveSec` is what creates the *recurring* tick after the
    # first fire. Without it the timer would fire once and stop.
    assert "OnUnitActiveSec" in parser["Timer"]
    assert parser["Timer"]["Unit"] == "fleetboot-heartbeat.service"
    assert parser["Install"]["WantedBy"] == "timers.target"


def test_recipe_enables_heartbeat_timer():
    """The recipe's `systemctl enable` block must include the timer, not
    just the service — only timers get fired periodically by systemd."""
    recipe = (REPO_ROOT / "image" / "fleetboot-base.yaml").read_text()
    assert "systemctl enable fleetboot-heartbeat.timer" in recipe
    # And the runtime helper must be installed + made executable.
    assert "/usr/local/lib/fleetboot" in recipe
    assert "fleetboot-heartbeat.sh" in recipe
