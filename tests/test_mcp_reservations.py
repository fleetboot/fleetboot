"""MCP-level tests for the reservation tools and availability field."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from fleetboot.server.app import create_app
from fleetboot.server.boot_sessions import BootSessionStore
from fleetboot.server.registry import MachineRegistry


ADMIN = "the-admin-secret"


class RecordingRun:
    def __init__(self, returncode: int = 0) -> None:
        self.commands: list = []
        self.returncode = returncode

    def __call__(self, command, **kwargs):
        self.commands.append(command)
        return subprocess.CompletedProcess(command, self.returncode)


@pytest.fixture
def no_real_subprocess(monkeypatch) -> RecordingRun:
    runner = RecordingRun()
    monkeypatch.setattr(subprocess, "run", runner)
    return runner


def _fleet(tmp_path: Path):
    root = tmp_path / "repo"
    for profile in ("claude-compute", "cinnamon-desktop", "default"):
        (root / "image" / "profiles" / profile).mkdir(parents=True)
    (root / "Makefile").write_text(".PHONY: image\nimage:\n\techo built\n")
    reg = MachineRegistry(tmp_path / "machines.sqlite")
    sessions = BootSessionStore(tmp_path / "sessions.sqlite")
    reg.set_setting("pdudaemon_host", "prowl:16421")
    app = create_app(
        sessions=sessions, registry=reg, admin_secret=ADMIN,
        dashboard_repo_root=root,
    )
    return TestClient(app), reg


def _call(client: TestClient, name: str, arguments: dict) -> dict:
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


def _pool_machine(reg: MachineRegistry, mac: str) -> None:
    reg.enroll(mac=mac, profile_name="default",
               architecture="x86_64", platform="efi")
    reg.set_pdu_alias(mac, mac.replace(":", ""))
    reg.release_to_pool(mac)


def _availability(client: TestClient, mac: str) -> str:
    machines = _call(client, "list_machines", {})["payload"]["machines"]
    return next(m["availability"] for m in machines if m["mac"] == mac)


def test_list_machines_exposes_availability(tmp_path: Path):
    client, reg = _fleet(tmp_path)
    reg.enroll(mac="aa:bb:cc:00:00:01", profile_name="default",
               architecture="x86_64", platform="efi")
    machines = _call(client, "list_machines", {})["payload"]["machines"]
    row = machines[0]
    # Enrolled machines are permanent by default (protected).
    assert row["availability"] == "permanent"
    assert row["reservation"]["lease_kind"] == "permanent"


def test_release_to_pool_then_reserve_then_release(
    tmp_path: Path, no_real_subprocess: RecordingRun,
):
    client, reg = _fleet(tmp_path)
    _pool_machine(reg, "aa:bb:cc:00:00:01")
    assert _availability(client, "aa:bb:cc:00:00:01") == "available"

    reserved = _call(client, "reserve_machines", {
        "profile": "claude-compute", "count": 1, "holder": "agent:k",
        "purpose": "kernel build", "ttl_minutes": 30,
    })["payload"]
    assert reserved["reserved"] == 1
    assert reserved["short_by"] == 0
    assert reserved["machines"][0]["availability"] == "reserved"
    assert reserved["machines"][0]["profile_name"] == "claude-compute"
    # It was powered on via pdudaemon.
    assert reserved["power_results"][0]["ok"] is True
    assert _availability(client, "aa:bb:cc:00:00:01") == "reserved"

    freed = _call(client, "release_machines", {"holder": "agent:k"})["payload"]
    assert freed["released"] == ["aa:bb:cc:00:00:01"]
    assert _availability(client, "aa:bb:cc:00:00:01") == "available"


def test_reserve_is_short_when_pool_empty(tmp_path: Path):
    client, reg = _fleet(tmp_path)
    reg.enroll(mac="aa:bb:cc:00:00:01", profile_name="default",
               architecture="x86_64", platform="efi")  # permanent, not pooled
    got = _call(client, "reserve_machines", {
        "profile": "claude-compute", "count": 3, "holder": "agent:x",
    })["payload"]
    assert got["reserved"] == 0
    assert got["short_by"] == 3


def test_reserve_rejects_unknown_profile(tmp_path: Path):
    client, reg = _fleet(tmp_path)
    _pool_machine(reg, "aa:bb:cc:00:00:01")
    got = _call(client, "reserve_machines", {
        "profile": "nope", "count": 1,
    })
    assert got["is_error"] is True
    assert "unknown profile" in got["payload"]["error"]


def test_renew_reservation_extends(tmp_path: Path, no_real_subprocess):
    client, reg = _fleet(tmp_path)
    _pool_machine(reg, "aa:bb:cc:00:00:01")
    _call(client, "reserve_machines", {
        "profile": "claude-compute", "count": 1, "holder": "a",
        "ttl_minutes": 10,
    })
    before = reg.active_reservation("aa:bb:cc:00:00:01").expires_at
    got = _call(client, "renew_reservation", {
        "mac": "aa:bb:cc:00:00:01", "ttl_minutes": 600,
    })["payload"]
    assert got["expires_at"] > before


def test_renew_errors_without_active_lease(tmp_path: Path):
    client, reg = _fleet(tmp_path)
    reg.enroll(mac="aa:bb:cc:00:00:01", profile_name="default",
               architecture="x86_64", platform="efi")  # permanent
    got = _call(client, "renew_reservation", {"mac": "aa:bb:cc:00:00:01"})
    assert got["is_error"] is True


def test_pin_machine_takes_it_out_of_the_pool(tmp_path: Path):
    client, reg = _fleet(tmp_path)
    _pool_machine(reg, "aa:bb:cc:00:00:01")
    assert _availability(client, "aa:bb:cc:00:00:01") == "available"
    _call(client, "pin_machine", {
        "mac": "aa:bb:cc:00:00:01", "holder": "school",
        "profile": "cinnamon-desktop",
    })
    assert _availability(client, "aa:bb:cc:00:00:01") == "permanent"
    assert reg.lookup("aa:bb:cc:00:00:01").profile_name == "cinnamon-desktop"


def test_release_to_pool_tool(tmp_path: Path):
    client, reg = _fleet(tmp_path)
    reg.enroll(mac="aa:bb:cc:00:00:01", profile_name="default",
               architecture="x86_64", platform="efi")
    got = _call(client, "release_to_pool", {"mac": "aa:bb:cc:00:00:01"})["payload"]
    assert got["released_permanent_hold"] is True
    assert got["availability"] == "available"
