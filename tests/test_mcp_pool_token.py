"""The pool-client MCP token: agents get compute, not fleet control.

An admin token grants the full tool set. A pool token grants only the
compute-pool lifecycle + read-only visibility, so an agent can reserve
and return workers but can never touch the permanent (school) fleet,
power arbitrary machines, edit profiles, or build.
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from fleetboot.server.app import create_app
from fleetboot.server.boot_sessions import BootSessionStore
from fleetboot.server.mcp import POOL_CLIENT_TOOLS
from fleetboot.server.registry import MachineRegistry


ADMIN = "the-admin-secret"
POOL = "the-pool-secret"


def _fleet(tmp_path: Path):
    root = tmp_path / "repo"
    (root / "image" / "profiles" / "claude-compute").mkdir(parents=True)
    (root / "image" / "profiles" / "default").mkdir(parents=True)
    (root / "Makefile").write_text(".PHONY: image\nimage:\n\techo built\n")
    reg = MachineRegistry(tmp_path / "machines.sqlite")
    app = create_app(
        sessions=BootSessionStore(tmp_path / "sessions.sqlite"),
        registry=reg, admin_secret=ADMIN, pool_secret=POOL,
        dashboard_repo_root=root,
    )
    return TestClient(app), reg


def _rpc(client: TestClient, token: str, method: str, params=None):
    body = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        body["params"] = params
    return client.post(
        "/mcp", json=body, headers={"Authorization": f"Bearer {token}"},
    )


def _call(client: TestClient, token: str, name: str, args: dict):
    return _rpc(client, token, "tools/call", {"name": name, "arguments": args})


def test_pool_token_tools_list_is_restricted(tmp_path: Path):
    client, _ = _fleet(tmp_path)
    admin_names = {
        t["name"] for t in
        _rpc(client, ADMIN, "tools/list").json()["result"]["tools"]
    }
    pool_names = {
        t["name"] for t in
        _rpc(client, POOL, "tools/list").json()["result"]["tools"]
    }
    assert pool_names == set(POOL_CLIENT_TOOLS)
    assert pool_names < admin_names  # strict subset
    # The dangerous ones are simply not visible to a pool client.
    for hidden in ("pin_machine", "release_to_pool", "enrol_machine",
                   "delete_machine", "start_build", "power_fleet"):
        assert hidden in admin_names
        assert hidden not in pool_names


def test_pool_token_can_reserve_and_release(tmp_path: Path):
    client, reg = _fleet(tmp_path)
    reg.enroll(mac="aa:bb:cc:00:00:01", profile_name="default",
               architecture="x86_64", platform="efi")
    reg.release_to_pool("aa:bb:cc:00:00:01")  # admin-side setup

    res = _call(client, POOL, "reserve_machines",
                {"profile": "claude-compute", "count": 1, "holder": "agent:x"})
    payload = json.loads(res.json()["result"]["content"][0]["text"])
    assert payload["reserved"] == 1

    rel = _call(client, POOL, "release_machines", {"holder": "agent:x"})
    freed = json.loads(rel.json()["result"]["content"][0]["text"])
    assert freed["released"] == ["aa:bb:cc:00:00:01"]


def test_pool_token_can_read_state(tmp_path: Path):
    client, _ = _fleet(tmp_path)
    for name, args in (
        ("describe_fleetboot", {"topic": "reservations"}),
        ("list_machines", {}),
        ("list_profiles", {}),
        ("recent_events", {}),
    ):
        r = _call(client, POOL, name, args)
        assert "result" in r.json(), (name, r.json())
        assert r.json()["result"]["isError"] is False


def test_pool_token_is_denied_admin_tools(tmp_path: Path):
    client, reg = _fleet(tmp_path)
    reg.enroll(mac="aa:bb:cc:00:00:01", profile_name="default",
               architecture="x86_64", platform="efi")
    for name, args in (
        ("release_to_pool", {"mac": "aa:bb:cc:00:00:01"}),
        ("pin_machine", {"mac": "aa:bb:cc:00:00:01"}),
        ("enrol_machine", {"mac": "aa:bb:cc:00:00:02",
                           "profile_name": "default"}),
        ("delete_machine", {"mac": "aa:bb:cc:00:00:01"}),
        ("set_machine_profile", {"mac": "aa:bb:cc:00:00:01",
                                 "profile_name": "claude-compute"}),
        ("power_machine", {"mac": "aa:bb:cc:00:00:01", "action": "off"}),
        ("power_fleet", {"action": "off", "profile": "default"}),
        ("start_build", {"profile": "default"}),
        ("save_profile", {"name": "evil"}),
    ):
        resp = _call(client, POOL, name, args).json()
        assert "error" in resp, (name, resp)
        assert "admin token" in resp["error"]["message"]
    # And the machine is untouched — the denied delete did nothing.
    assert reg.lookup("aa:bb:cc:00:00:01") is not None


def test_admin_token_still_has_everything(tmp_path: Path):
    client, reg = _fleet(tmp_path)
    reg.enroll(mac="aa:bb:cc:00:00:01", profile_name="default",
               architecture="x86_64", platform="efi")
    # An admin-only tool works with the admin token.
    resp = _call(client, ADMIN, "release_to_pool",
                 {"mac": "aa:bb:cc:00:00:01"}).json()
    assert resp["result"]["isError"] is False


def test_unknown_token_rejected(tmp_path: Path):
    client, _ = _fleet(tmp_path)
    assert _rpc(client, "not-a-real-token", "tools/list").status_code == 401


def test_pool_token_disabled_when_unset(tmp_path: Path):
    """With no pool_secret configured, the pool token is just an unknown
    token — only the admin token authenticates."""
    root = tmp_path / "repo"
    (root / "image" / "profiles" / "default").mkdir(parents=True)
    (root / "Makefile").write_text(".PHONY: image\nimage:\n\techo built\n")
    app = create_app(
        sessions=BootSessionStore(),
        registry=MachineRegistry(tmp_path / "m.sqlite"),
        admin_secret=ADMIN, dashboard_repo_root=root,  # no pool_secret
    )
    client = TestClient(app)
    assert _rpc(client, POOL, "tools/list").status_code == 401
    assert _rpc(client, ADMIN, "tools/list").status_code == 200
