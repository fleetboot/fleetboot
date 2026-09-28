"""Tests for power control: on, off, reboot — and the on-demand pool.

Power is the one part of the control plane that reaches out and touches
physical hardware, so most of these tests pin down *which* command
fleetboot would run rather than running anything: a recorded shell
command is the whole contract with pdudaemon.
"""

from __future__ import annotations

import base64
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from fleetboot.server.app import create_app
from fleetboot.server.boot_sessions import BootSessionStore
from fleetboot.server.dashboard import power_machine, reboot_machine
from fleetboot.server.registry import MachineRegistry


ADMIN = "the-admin-secret"
MAC = "aa:bb:cc:dd:ee:01"


class RecordingRun:
    """Stand-in for subprocess.run that records commands instead of
    running them. `returncode` decides whether the 'PDU' accepted."""

    def __init__(self, returncode: int = 0) -> None:
        self.commands: list[str] = []
        self.returncode = returncode

    def __call__(self, command, **kwargs):
        self.commands.append(command)
        return subprocess.CompletedProcess(command, self.returncode)

    @property
    def only_command(self) -> str:
        assert len(self.commands) == 1, self.commands
        return self.commands[0]


@pytest.fixture
def registry(tmp_path: Path) -> MachineRegistry:
    reg = MachineRegistry(tmp_path / "machines.sqlite")
    reg.enroll(
        mac=MAC, profile_name="render-node",
        architecture="x86_64", platform="efi",
    )
    return reg


@pytest.fixture
def no_real_subprocess(monkeypatch) -> RecordingRun:
    runner = RecordingRun()
    monkeypatch.setattr(subprocess, "run", runner)
    return runner


# ---- Registry: storing the wiring ---------------------------------------


def test_power_commands_round_trip(registry: MachineRegistry):
    registry.set_power_command(MAC, "on", "switch-on.sh")
    registry.set_power_command(MAC, "off", "switch-off.sh")
    registry.set_power_command(MAC, "reboot", "cycle.sh")
    machine = registry.lookup(MAC)
    assert machine.power_on_command == "switch-on.sh"
    assert machine.power_off_command == "switch-off.sh"
    assert machine.reboot_command == "cycle.sh"

    registry.set_power_command(MAC, "on", "")
    assert registry.lookup(MAC).power_on_command is None


def test_set_reboot_command_still_writes_the_reboot_slot(
    registry: MachineRegistry,
):
    """The older, narrower method has to keep working — the dashboard
    and the set_reboot_command MCP tool both call it."""
    registry.set_reboot_command(MAC, "cycle.sh")
    assert registry.lookup(MAC).reboot_command == "cycle.sh"


def test_set_power_command_rejects_an_unknown_action(
    registry: MachineRegistry,
):
    with pytest.raises(ValueError, match="unknown power action"):
        registry.set_power_command(MAC, "explode", "rm -rf /")


def test_pdu_alias_round_trips_and_clears(registry: MachineRegistry):
    registry.set_pdu_alias(MAC, "rack1-port7")
    assert registry.lookup(MAC).pdu_alias == "rack1-port7"
    registry.set_pdu_alias(MAC, "  ")
    assert registry.lookup(MAC).pdu_alias is None


def test_re_enrolling_preserves_power_wiring(registry: MachineRegistry):
    """Re-enrolment replaces the row wholesale. Power wiring is
    physical — it survives a profile change — and a machine you can no
    longer switch on is a machine you have lost."""
    registry.set_pdu_alias(MAC, "rack1-port7")
    registry.set_power_command(MAC, "on", "switch-on.sh")
    registry.set_power_command(MAC, "off", "switch-off.sh")
    registry.set_reboot_command(MAC, "cycle.sh")

    registry.enroll(
        mac=MAC, profile_name="cinnamon-desktop",
        architecture="x86_64", platform="efi",
    )

    machine = registry.lookup(MAC)
    assert machine.profile_name == "cinnamon-desktop"
    assert machine.pdu_alias == "rack1-port7"
    assert machine.power_on_command == "switch-on.sh"
    assert machine.power_off_command == "switch-off.sh"
    assert machine.reboot_command == "cycle.sh"


# ---- The power core -----------------------------------------------------


@pytest.mark.parametrize("action,command", [
    ("on", "switch-on.sh"),
    ("off", "switch-off.sh"),
    ("reboot", "cycle.sh"),
])
def test_machine_command_wins_over_pdudaemon(
    registry: MachineRegistry, no_real_subprocess: RecordingRun,
    action: str, command: str,
):
    registry.set_setting("pdudaemon_host", "prowl:16421")
    registry.update_hostname(MAC, "node-07")
    registry.set_power_command(MAC, action, command)

    result = power_machine(registry, MAC, action)

    assert no_real_subprocess.only_command == command
    assert result.method == "machine_command"
    assert result.ok is True


@pytest.mark.parametrize("action", ["on", "off", "reboot"])
def test_pdudaemon_url_carries_the_action_and_alias(
    registry: MachineRegistry, no_real_subprocess: RecordingRun,
    action: str,
):
    registry.set_setting("pdudaemon_host", "prowl:16421")
    registry.update_hostname(MAC, "node-07")

    result = power_machine(registry, MAC, action)

    assert no_real_subprocess.only_command == (
        'curl --fail "http://prowl:16421/power/control/'
        f'{action}?alias=node-07"'
    )
    assert result.method == "pdudaemon"
    assert result.ok is True


def test_pdu_alias_overrides_the_reported_hostname(
    registry: MachineRegistry, no_real_subprocess: RecordingRun,
):
    registry.set_setting("pdudaemon_host", "prowl:16421")
    registry.update_hostname(MAC, "node-07")
    registry.set_pdu_alias(MAC, "rack1-port7")

    power_machine(registry, MAC, "on")

    assert "alias=rack1-port7" in no_real_subprocess.only_command


def test_alias_works_before_a_machine_has_ever_booted(
    registry: MachineRegistry, no_real_subprocess: RecordingRun,
):
    """The on-demand cold start: no hostname has ever been reported,
    because the machine has never been on."""
    registry.set_setting("pdudaemon_host", "prowl:16421")
    registry.set_pdu_alias(MAC, "rack1-port7")
    assert registry.lookup(MAC).hostname is None

    result = power_machine(registry, MAC, "on")

    assert result.ok is True
    assert "alias=rack1-port7" in no_real_subprocess.only_command


def test_power_on_with_no_wiring_at_all_fails_visibly(
    registry: MachineRegistry, no_real_subprocess: RecordingRun,
):
    result = power_machine(registry, MAC, "on")

    assert no_real_subprocess.commands == []
    assert result.ok is False
    assert result.method == "none"
    assert "pdudaemon_host" in result.detail


def test_power_on_without_an_alias_says_so(
    registry: MachineRegistry, no_real_subprocess: RecordingRun,
):
    registry.set_setting("pdudaemon_host", "prowl:16421")

    result = power_machine(registry, MAC, "on")

    assert result.ok is False
    assert "pdu_alias" in result.detail


def test_a_refused_power_off_does_not_arm_a_soft_reboot(
    registry: MachineRegistry, monkeypatch,
):
    """There is no soft power-off, so a failed `off` must report failure
    rather than quietly rebooting the machine instead."""
    monkeypatch.setattr(subprocess, "run", RecordingRun(returncode=22))
    registry.set_setting("pdudaemon_host", "prowl:16421")
    registry.update_hostname(MAC, "node-07")

    result = power_machine(registry, MAC, "off")

    assert result.ok is False
    assert "exited 22" in result.detail
    assert registry.lookup(MAC).pending_reboot is False


def test_a_refused_reboot_falls_back_to_the_soft_signal(
    registry: MachineRegistry, monkeypatch,
):
    monkeypatch.setattr(subprocess, "run", RecordingRun(returncode=22))
    registry.set_setting("pdudaemon_host", "prowl:16421")
    registry.update_hostname(MAC, "node-07")

    result = power_machine(registry, MAC, "reboot")

    assert result.method == "soft_reboot"
    assert result.ok is True
    assert registry.lookup(MAC).pending_reboot is True


class TimeoutRecordingRun(RecordingRun):
    """RecordingRun that also remembers the timeout each command got."""

    def __init__(self, returncode: int = 0) -> None:
        super().__init__(returncode)
        self.timeouts: list[float] = []

    def __call__(self, command, **kwargs):
        self.timeouts.append(kwargs.get("timeout"))
        return super().__call__(command, **kwargs)


def test_power_commands_get_a_generous_default_timeout(
    registry: MachineRegistry, monkeypatch,
):
    """pdudaemon answers only after driving the PDU's telnet menu, which
    takes several seconds on a healthy APC — the default must allow it."""
    monkeypatch.delenv("FLEETBOOT_POWER_TIMEOUT", raising=False)
    runner = TimeoutRecordingRun()
    monkeypatch.setattr(subprocess, "run", runner)
    registry.set_setting("pdudaemon_host", "prowl:16421")
    registry.update_hostname(MAC, "node-07")

    power_machine(registry, MAC, "on")

    assert runner.timeouts == [60]


def test_power_timeout_can_be_set_from_the_environment(
    registry: MachineRegistry, monkeypatch,
):
    monkeypatch.setenv("FLEETBOOT_POWER_TIMEOUT", "90")
    runner = TimeoutRecordingRun()
    monkeypatch.setattr(subprocess, "run", runner)
    registry.set_setting("pdudaemon_host", "prowl:16421")
    registry.update_hostname(MAC, "node-07")

    power_machine(registry, MAC, "on")

    assert runner.timeouts == [90]


@pytest.mark.parametrize("bad_value", ["", "soon", "0", "-5"])
def test_a_nonsense_power_timeout_falls_back_to_the_default(
    registry: MachineRegistry, monkeypatch, bad_value: str,
):
    """A typo must not make power control hang forever or fail instantly."""
    monkeypatch.setenv("FLEETBOOT_POWER_TIMEOUT", bad_value)
    runner = TimeoutRecordingRun()
    monkeypatch.setattr(subprocess, "run", runner)
    registry.set_setting("pdudaemon_host", "prowl:16421")
    registry.update_hostname(MAC, "node-07")

    power_machine(registry, MAC, "on")

    assert runner.timeouts == [60]


def test_a_timed_out_power_command_reports_the_timeout_it_used(
    registry: MachineRegistry, monkeypatch,
):
    monkeypatch.setenv("FLEETBOOT_POWER_TIMEOUT", "45")

    def slow_pdu(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", slow_pdu)
    registry.set_setting("pdudaemon_host", "prowl:16421")
    registry.update_hostname(MAC, "node-07")

    result = power_machine(registry, MAC, "on")

    assert result.ok is False
    assert result.detail == "pdudaemon timed out after 45s"


def test_reboot_machine_still_deletes_when_asked(
    registry: MachineRegistry, no_real_subprocess: RecordingRun,
):
    registry.set_reboot_command(MAC, "cycle.sh")

    result = reboot_machine(registry, MAC, then_delete=True)

    assert result.deleted is True
    assert registry.lookup(MAC) is None


def test_powering_an_unknown_mac_reports_failure(
    registry: MachineRegistry, no_real_subprocess: RecordingRun,
):
    result = power_machine(registry, "ff:ff:ff:ff:ff:ff", "on")
    assert result.ok is False
    assert "no machine" in result.detail


def test_power_machine_rejects_an_unknown_action(
    registry: MachineRegistry,
):
    with pytest.raises(ValueError, match="unknown power action"):
        power_machine(registry, MAC, "explode")


# ---- Dashboard buttons --------------------------------------------------


def _fleet(tmp_path: Path) -> tuple[TestClient, MachineRegistry,
                                    BootSessionStore]:
    """A client wired to a persistent session store — the in-memory one
    keeps no timestamps, so nothing would ever look online."""
    root = tmp_path / "repo"
    (root / "image" / "profiles" / "render-node").mkdir(parents=True)
    (root / "Makefile").write_text(".PHONY: image\nimage:\n\techo built\n")
    reg = MachineRegistry(tmp_path / "machines.sqlite")
    sessions = BootSessionStore(tmp_path / "sessions.sqlite")
    app = create_app(
        sessions=sessions, registry=reg, admin_secret=ADMIN,
        dashboard_repo_root=root,
    )
    return TestClient(app), reg, sessions


def _basic_auth() -> dict[str, str]:
    token = base64.b64encode(f"admin:{ADMIN}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def test_dashboard_power_button_fires_pdudaemon(
    tmp_path: Path, no_real_subprocess: RecordingRun,
):
    client, reg, _ = _fleet(tmp_path)
    reg.enroll(mac=MAC, profile_name="render-node",
               architecture="x86_64", platform="efi")
    reg.set_setting("pdudaemon_host", "prowl:16421")
    reg.set_pdu_alias(MAC, "rack1-port7")

    response = client.post(
        f"/dashboard/machines/{MAC}/power/off",
        headers=_basic_auth(), follow_redirects=False,
    )

    assert response.status_code == 303
    assert "/power/control/off?alias=rack1-port7" in \
        no_real_subprocess.only_command


def test_dashboard_rejects_an_unknown_power_action(tmp_path: Path):
    client, reg, _ = _fleet(tmp_path)
    reg.enroll(mac=MAC, profile_name="render-node",
               architecture="x86_64", platform="efi")

    response = client.post(
        f"/dashboard/machines/{MAC}/power/explode", headers=_basic_auth(),
    )

    assert response.status_code == 400


def test_dashboard_saves_power_wiring(tmp_path: Path):
    client, reg, _ = _fleet(tmp_path)
    reg.enroll(mac=MAC, profile_name="render-node",
               architecture="x86_64", platform="efi")

    client.post(
        f"/dashboard/machines/{MAC}/power-settings",
        data={
            "pdu_alias": "rack1-port7",
            "power_on_command": "switch-on.sh",
            "power_off_command": "switch-off.sh",
        },
        headers=_basic_auth(), follow_redirects=False,
    )

    machine = reg.lookup(MAC)
    assert machine.pdu_alias == "rack1-port7"
    assert machine.power_on_command == "switch-on.sh"
    assert machine.power_off_command == "switch-off.sh"
