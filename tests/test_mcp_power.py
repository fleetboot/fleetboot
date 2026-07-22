"""Tests for the MCP power tools — per-machine and fleet-wide.

These are the on-demand compute lever: an agent asks for capacity by
profile and count, and has to be able to tell what it actually got.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from fleetboot.boot_states import BootState
from fleetboot.server.app import create_app
from fleetboot.server.boot_sessions import BootSessionStore
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
def no_real_subprocess(monkeypatch) -> RecordingRun:
    runner = RecordingRun()
    monkeypatch.setattr(subprocess, "run", runner)
    return runner


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



def _call_tool(client: TestClient, name: str, arguments: dict) -> dict:
    response = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
        headers={"Authorization": f"Bearer {ADMIN}"},
    )
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    return {
        "payload": json.loads(result["content"][0]["text"]),
        "is_error": result["isError"],
    }


def _bring_online(sessions: BootSessionStore, mac: str) -> None:
    """Make a machine look like it is up and running: a live boot
    session that has reached the greeter."""
    session = sessions.mint(mac)
    sessions.record_state(session.token, BootState.NETWORK_UP)
    sessions.record_state(session.token, BootState.LOGIN_CONSOLE)


def test_power_machine_tool_reports_the_route_taken(
    tmp_path: Path, no_real_subprocess: RecordingRun,
):
    client, reg, _ = _fleet(tmp_path)
    reg.enroll(mac=MAC, profile_name="render-node",
               architecture="x86_64", platform="efi")
    reg.set_setting("pdudaemon_host", "prowl:16421")
    reg.set_pdu_alias(MAC, "rack1-port7")

    got = _call_tool(client, "power_machine", {"mac": MAC, "action": "on"})

    assert got["payload"] == {
        "mac": MAC, "action": "on", "method": "pdudaemon",
        "ok": True, "detail": "pdudaemon succeeded", "deleted": False,
    }


def test_power_machine_tool_rejects_bad_input(tmp_path: Path):
    client, reg, _ = _fleet(tmp_path)
    reg.enroll(mac=MAC, profile_name="render-node",
               architecture="x86_64", platform="efi")

    bad_action = _call_tool(
        client, "power_machine", {"mac": MAC, "action": "explode"},
    )
    assert bad_action["is_error"] is True

    unknown = _call_tool(
        client, "power_machine",
        {"mac": "ff:ff:ff:ff:ff:ff", "action": "on"},
    )
    assert unknown["is_error"] is True
    assert "no machine" in unknown["payload"]["error"]


def test_power_wiring_tools_write_the_registry(tmp_path: Path):
    client, reg, _ = _fleet(tmp_path)
    reg.enroll(mac=MAC, profile_name="render-node",
               architecture="x86_64", platform="efi")

    _call_tool(client, "set_pdu_alias", {"mac": MAC, "alias": "rack1-port7"})
    _call_tool(client, "set_power_command", {
        "mac": MAC, "action": "off", "command": "ipmitool power off",
    })

    machine = reg.lookup(MAC)
    assert machine.pdu_alias == "rack1-port7"
    assert machine.power_off_command == "ipmitool power off"


def test_list_machines_reports_liveness(tmp_path: Path):
    client, reg, sessions = _fleet(tmp_path)
    reg.enroll(mac=MAC, profile_name="render-node",
               architecture="x86_64", platform="efi")
    reg.enroll(mac="aa:bb:cc:dd:ee:02", profile_name="render-node",
               architecture="x86_64", platform="efi")
    _bring_online(sessions, MAC)

    machines = {
        m["mac"]: m
        for m in _call_tool(client, "list_machines", {})["payload"]["machines"]
    }

    assert machines[MAC]["online"] is True
    assert machines[MAC]["state"] == "login_console"
    assert machines[MAC]["seconds_since_last_seen"] < 60
    # Never booted, never heard from.
    assert machines["aa:bb:cc:dd:ee:02"]["online"] is False
    assert machines["aa:bb:cc:dd:ee:02"]["state"] is None


# ---- On-demand compute: power_fleet -------------------------------------


def _pool(tmp_path: Path, count: int = 4, profile: str = "render-node"):
    client, reg, sessions = _fleet(tmp_path)
    reg.set_setting("pdudaemon_host", "prowl:16421")
    macs = []
    for index in range(count):
        mac = f"aa:bb:cc:dd:ee:{index:02d}"
        reg.enroll(mac=mac, profile_name=profile,
                   architecture="x86_64", platform="efi")
        reg.set_pdu_alias(mac, f"rack1-port{index}")
        macs.append(mac)
    return client, reg, sessions, macs


def test_power_fleet_switches_on_a_requested_count(
    tmp_path: Path, no_real_subprocess: RecordingRun,
):
    client, _, _, _ = _pool(tmp_path, count=4)

    got = _call_tool(client, "power_fleet", {
        "action": "on", "profile": "render-node", "count": 2,
    })["payload"]

    assert got["matching"] == 4
    assert got["eligible"] == 4
    assert got["selected"] == 2
    assert got["succeeded"] == 2
    assert got["short_by"] == 0
    assert len(no_real_subprocess.commands) == 2
    assert all("/power/control/on?" in c for c in no_real_subprocess.commands)


def test_power_fleet_ignores_other_profiles(
    tmp_path: Path, no_real_subprocess: RecordingRun,
):
    client, reg, _, _ = _pool(tmp_path, count=2)
    reg.enroll(mac="aa:bb:cc:dd:ee:99", profile_name="cinnamon-desktop",
               architecture="x86_64", platform="efi")
    reg.set_pdu_alias("aa:bb:cc:dd:ee:99", "desk-1")

    got = _call_tool(client, "power_fleet", {
        "action": "on", "profile": "render-node",
    })["payload"]

    assert got["matching"] == 2
    assert {r["mac"] for r in got["results"]} == {
        "aa:bb:cc:dd:ee:00", "aa:bb:cc:dd:ee:01",
    }


def test_power_fleet_on_skips_machines_already_up(
    tmp_path: Path, no_real_subprocess: RecordingRun,
):
    """Asking twice for two workers must not cycle the two you have."""
    client, _, sessions, macs = _pool(tmp_path, count=4)
    _bring_online(sessions, macs[0])
    _bring_online(sessions, macs[1])

    got = _call_tool(client, "power_fleet", {
        "action": "on", "profile": "render-node", "count": 4,
    })["payload"]

    assert got["eligible"] == 2
    assert got["selected"] == 2
    assert got["short_by"] == 2
    assert {r["mac"] for r in got["results"]} == {macs[2], macs[3]}


def test_power_fleet_off_targets_only_machines_that_are_up(
    tmp_path: Path, no_real_subprocess: RecordingRun,
):
    client, _, sessions, macs = _pool(tmp_path, count=4)
    _bring_online(sessions, macs[2])

    got = _call_tool(client, "power_fleet", {
        "action": "off", "profile": "render-node",
    })["payload"]

    assert got["selected"] == 1
    assert got["results"][0]["mac"] == macs[2]
    assert "/power/control/off?alias=rack1-port2" in \
        no_real_subprocess.only_command


def test_power_fleet_filters_by_architecture(
    tmp_path: Path, no_real_subprocess: RecordingRun,
):
    client, reg, _, _ = _pool(tmp_path, count=2)
    reg.enroll(mac="aa:bb:cc:dd:ee:80", profile_name="render-node",
               architecture="arm64", platform="efi")
    reg.set_pdu_alias("aa:bb:cc:dd:ee:80", "pi-1")

    # amd64 and x86_64 name the same architecture, so the two x86 boxes
    # match and the arm64 one does not.
    got = _call_tool(client, "power_fleet", {
        "action": "on", "profile": "render-node", "architecture": "amd64",
    })["payload"]

    assert got["matching"] == 2
    assert "aa:bb:cc:dd:ee:80" not in {r["mac"] for r in got["results"]}


def test_power_fleet_reports_machines_it_could_not_switch(
    tmp_path: Path, monkeypatch,
):
    monkeypatch.setattr(subprocess, "run", RecordingRun(returncode=7))
    client, _, _, _ = _pool(tmp_path, count=2)

    got = _call_tool(client, "power_fleet", {
        "action": "on", "profile": "render-node",
    })["payload"]

    assert got["selected"] == 2
    assert got["succeeded"] == 0
    assert all(r["ok"] is False for r in got["results"])


def test_power_fleet_validates_its_arguments(tmp_path: Path):
    client, _, _, _ = _pool(tmp_path, count=1)

    for arguments in (
        {"action": "explode", "profile": "render-node"},
        {"action": "on", "profile": "render-node", "count": 0},
        {"action": "on", "profile": "render-node", "count": "two"},
    ):
        assert _call_tool(client, "power_fleet", arguments)["is_error"] is True
