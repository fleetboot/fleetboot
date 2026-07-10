"""Tests for the /mcp endpoint (MCP Streamable HTTP transport)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from fleetboot.server.app import create_app
from fleetboot.server.boot_sessions import BootSessionStore
from fleetboot.server.registry import MachineRegistry


ADMIN = "the-admin-secret"


@pytest.fixture
def mcp_root(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "Makefile").write_text(".PHONY: image\nimage:\n\techo built\n")
    profiles = root / "image" / "profiles"
    profiles.mkdir(parents=True)
    (profiles / "default").mkdir()
    (profiles / "default" / "extra-packages.list").write_text("")
    (profiles / "default" / "README.md").write_text("# default\n")
    return root


def _client(mcp_root: Path) -> TestClient:
    registry = MachineRegistry(mcp_root / "machines.sqlite")
    app = create_app(
        sessions=BootSessionStore(),
        registry=registry,
        admin_secret=ADMIN,
        dashboard_repo_root=mcp_root,
    )
    return TestClient(app)


def _bearer(secret: str = ADMIN) -> dict[str, str]:
    return {"Authorization": f"Bearer {secret}"}


def _rpc(client: TestClient, method: str, params: dict | None = None,
         *, id_: int | str = 1, secret: str = ADMIN):
    body: dict = {"jsonrpc": "2.0", "id": id_, "method": method}
    if params is not None:
        body["params"] = params
    return client.post("/mcp", json=body, headers=_bearer(secret))


def _call_tool(client: TestClient, name: str, arguments: dict | None = None,
               *, id_: int | str = 1) -> dict:
    """Invoke a tool and return the parsed JSON payload from its text
    content block. Tests care about the semantic result, not the MCP
    envelope."""
    resp = _rpc(client, "tools/call",
                {"name": name, "arguments": arguments or {}}, id_=id_)
    assert resp.status_code == 200, resp.text
    envelope = resp.json()
    assert envelope.get("id") == id_
    assert "result" in envelope, envelope
    result = envelope["result"]
    text = result["content"][0]["text"]
    return json.loads(text)


# ---- Auth ---------------------------------------------------------------


def test_mcp_requires_bearer_token(mcp_root: Path):
    client = _client(mcp_root)
    resp = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert resp.status_code == 401


def test_mcp_rejects_wrong_secret(mcp_root: Path):
    client = _client(mcp_root)
    resp = client.post(
        "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
        headers=_bearer("no"),
    )
    assert resp.status_code == 401


def test_mcp_accepts_correct_bearer(mcp_root: Path):
    client = _client(mcp_root)
    resp = _rpc(client, "ping")
    assert resp.status_code == 200
    assert resp.json() == {"jsonrpc": "2.0", "id": 1, "result": {}}


# ---- Handshake ----------------------------------------------------------


def test_initialize_returns_server_info_and_capabilities(mcp_root: Path):
    client = _client(mcp_root)
    resp = _rpc(client, "initialize", {"protocolVersion": "2025-06-18",
                                       "clientInfo": {"name": "test"}})
    assert resp.status_code == 200
    result = resp.json()["result"]
    assert result["serverInfo"]["name"] == "fleetboot"
    assert "protocolVersion" in result
    assert "tools" in result["capabilities"]


def test_initialize_ships_instructions_for_the_model(mcp_root: Path):
    """Top-level `instructions` is the MCP way for a server to explain
    what it is and how to use its tools together. Without it, an agent
    only sees per-tool descriptions and has to reverse-engineer the
    workflow (build → reboot → PXE)."""
    client = _client(mcp_root)
    result = _rpc(client, "initialize", {"protocolVersion": "2025-06-18",
                                         "clientInfo": {"name": "test"}}
                  ).json()["result"]
    assert "instructions" in result
    instructions = result["instructions"]
    assert "profile" in instructions.lower()
    assert "build" in instructions.lower()
    assert "reboot" in instructions.lower()


def test_initialized_notification_returns_202(mcp_root: Path):
    client = _client(mcp_root)
    resp = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        headers=_bearer(),
    )
    assert resp.status_code == 202


# ---- Tool catalogue -----------------------------------------------------


def test_tools_list_advertises_expected_tools(mcp_root: Path):
    client = _client(mcp_root)
    resp = _rpc(client, "tools/list")
    tools = resp.json()["result"]["tools"]
    names = {t["name"] for t in tools}
    for expected in (
        "list_machines", "enrol_machine", "delete_machine",
        "reboot_machine", "set_pending_reboot",
        "list_profiles", "save_profile", "write_profile_overlay",
        "start_build", "list_builds", "get_build", "recent_events",
    ):
        assert expected in names, f"missing tool: {expected}"
    for t in tools:
        assert "inputSchema" in t
        assert t["inputSchema"]["type"] == "object"


# ---- Machine tools ------------------------------------------------------


def test_enrol_then_list_and_get_machine(mcp_root: Path):
    client = _client(mcp_root)
    _call_tool(client, "enrol_machine", {
        "mac": "aa:bb:cc:dd:ee:01",
        "profile_name": "default",
        "architecture": "x86_64",
        "platform": "efi",
    })
    listed = _call_tool(client, "list_machines")
    macs = [m["mac"] for m in listed["machines"]]
    assert "aa:bb:cc:dd:ee:01" in macs

    got = _call_tool(client, "get_machine", {"mac": "aa:bb:cc:dd:ee:01"})
    assert got["profile_name"] == "default"
    assert got["pending_reboot"] is False


def test_reboot_without_pdu_arms_soft_reboot(mcp_root: Path):
    client = _client(mcp_root)
    _call_tool(client, "enrol_machine", {
        "mac": "aa:bb:cc:dd:ee:02",
        "profile_name": "default",
    })
    out = _call_tool(client, "reboot_machine",
                     {"mac": "aa:bb:cc:dd:ee:02"})
    assert out["deleted"] is False
    assert out["pending_reboot"] is True


def test_reboot_with_delete_after_removes_row(mcp_root: Path):
    client = _client(mcp_root)
    _call_tool(client, "enrol_machine", {
        "mac": "aa:bb:cc:dd:ee:03",
        "profile_name": "default",
    })
    out = _call_tool(client, "reboot_machine",
                     {"mac": "aa:bb:cc:dd:ee:03", "delete_after": True})
    assert out["deleted"] is True

    listed = _call_tool(client, "list_machines")
    macs = [m["mac"] for m in listed["machines"]]
    assert "aa:bb:cc:dd:ee:03" not in macs


def test_set_pending_reboot_tool(mcp_root: Path):
    client = _client(mcp_root)
    _call_tool(client, "enrol_machine", {
        "mac": "aa:bb:cc:dd:ee:04",
        "profile_name": "default",
    })
    out = _call_tool(client, "set_pending_reboot", {
        "mac": "aa:bb:cc:dd:ee:04", "armed": True,
    })
    assert out["pending_reboot"] is True

    got = _call_tool(client, "get_machine", {"mac": "aa:bb:cc:dd:ee:04"})
    assert got["pending_reboot"] is True


# ---- Profile tools ------------------------------------------------------


def test_list_profiles_returns_seeded_profiles(mcp_root: Path):
    client = _client(mcp_root)
    listed = _call_tool(client, "list_profiles")
    assert "default" in listed["profiles"]


def test_save_profile_creates_new_profile_on_disk(mcp_root: Path):
    client = _client(mcp_root)
    out = _call_tool(client, "save_profile", {
        "name": "new-classroom",
        "extra_packages": "vim\n",
        "readme": "# new-classroom\nAdds vim.\n",
    })
    assert out["created"] is True

    pdir = mcp_root / "image" / "profiles" / "new-classroom"
    assert (pdir / "extra-packages.list").read_text() == "vim\n"
    assert (pdir / "README.md").read_text().startswith("# new-classroom")

    listed = _call_tool(client, "list_profiles")
    assert "new-classroom" in listed["profiles"]


def test_save_profile_updates_existing_profile(mcp_root: Path):
    client = _client(mcp_root)
    _call_tool(client, "save_profile", {
        "name": "default",
        "extra_packages": "curl\n",
    })
    got = _call_tool(client, "get_profile", {"name": "default"})
    assert got["extra_packages"] == "curl\n"


def test_write_profile_overlay_creates_nested_file(mcp_root: Path):
    client = _client(mcp_root)
    out = _call_tool(client, "write_profile_overlay", {
        "name": "default",
        "relpath": "etc/hostname",
        "content": "classroom-01\n",
    })
    assert out["bytes_written"] == len("classroom-01\n")
    written = mcp_root / "image" / "profiles" / "default" / "overlay" / "etc" / "hostname"
    assert written.read_text() == "classroom-01\n"


# ---- Build tools --------------------------------------------------------


def test_list_builds_starts_empty(mcp_root: Path):
    client = _client(mcp_root)
    out = _call_tool(client, "list_builds")
    assert out["running"] is False
    assert out["jobs"] == []


def test_start_build_registers_job(mcp_root: Path):
    client = _client(mcp_root)
    started = _call_tool(client, "start_build", {"profile": "default"})
    assert started["profile"] == "default"
    job_id = started["job_id"]

    # Wait for the tiny `echo built` subprocess to finish.
    import time
    for _ in range(50):
        got = _call_tool(client, "get_build", {"job_id": job_id})
        if got["state"] in ("succeeded", "failed"):
            break
        time.sleep(0.1)
    assert got["state"] in ("succeeded", "failed")


def test_start_build_with_auto_reboot_arms_matching_machines(mcp_root: Path):
    """Fleet-wide reboot: a successful build with auto_reboot=true
    arms `pending_reboot` on every machine running the same
    profile+arch. Uses soft-reboot (not PDU) because a herd of curl
    commands against one PDU is a bad time."""
    import time

    client = _client(mcp_root)
    # Two machines on the target profile, one on a different profile —
    # only the matching pair should get armed.
    _call_tool(client, "enrol_machine", {
        "mac": "aa:bb:cc:dd:ee:10", "profile_name": "default",
        "architecture": "x86_64",
    })
    _call_tool(client, "enrol_machine", {
        "mac": "aa:bb:cc:dd:ee:11", "profile_name": "default",
        "architecture": "x86_64",
    })
    # This one should NOT be armed: different profile.
    _call_tool(client, "enrol_machine", {
        "mac": "aa:bb:cc:dd:ee:12", "profile_name": "default",
        "architecture": "arm64",
    })
    # Create a second profile so the third machine is truly on a
    # different profile than the one we're building.
    (mcp_root / "image" / "profiles" / "kiosk").mkdir()
    (mcp_root / "image" / "profiles" / "kiosk" / "extra-packages.list"
     ).write_text("")
    _call_tool(client, "enrol_machine", {
        "mac": "aa:bb:cc:dd:ee:13", "profile_name": "kiosk",
        "architecture": "x86_64",
    })

    started = _call_tool(client, "start_build", {
        "profile": "default", "architecture": "amd64",
        "auto_reboot": True,
    })
    assert started["auto_reboot"] is True
    job_id = started["job_id"]

    for _ in range(50):
        got = _call_tool(client, "get_build", {"job_id": job_id})
        if got["state"] in ("succeeded", "failed"):
            break
        time.sleep(0.1)
    assert got["state"] == "succeeded"
    # The sweep count reported on the job matches only the two matching
    # machines. The arm64 default machine and the kiosk machine
    # deliberately stayed cold.
    assert got["auto_reboot_armed"] == 2

    def _pending(mac: str) -> bool:
        return _call_tool(
            client, "get_machine", {"mac": mac},
        )["pending_reboot"]

    assert _pending("aa:bb:cc:dd:ee:10") is True
    assert _pending("aa:bb:cc:dd:ee:11") is True
    assert _pending("aa:bb:cc:dd:ee:12") is False
    assert _pending("aa:bb:cc:dd:ee:13") is False


def test_start_build_without_auto_reboot_leaves_machines_untouched(
    mcp_root: Path,
):
    """Opt-in: a build without auto_reboot must not touch any
    machine's pending_reboot flag."""
    import time

    client = _client(mcp_root)
    _call_tool(client, "enrol_machine", {
        "mac": "aa:bb:cc:dd:ee:20", "profile_name": "default",
        "architecture": "x86_64",
    })
    started = _call_tool(client, "start_build", {"profile": "default"})
    job_id = started["job_id"]
    for _ in range(50):
        got = _call_tool(client, "get_build", {"job_id": job_id})
        if got["state"] in ("succeeded", "failed"):
            break
        time.sleep(0.1)
    assert got["auto_reboot"] is False
    assert got["auto_reboot_armed"] is None
    m = _call_tool(client, "get_machine", {"mac": "aa:bb:cc:dd:ee:20"})
    assert m["pending_reboot"] is False


def test_start_build_rejects_unknown_profile(mcp_root: Path):
    client = _client(mcp_root)
    resp = _rpc(client, "tools/call", {
        "name": "start_build",
        "arguments": {"profile": "nope"},
    })
    result = resp.json()["result"]
    assert result["isError"] is True
    payload = json.loads(result["content"][0]["text"])
    assert "profile not found" in payload["error"]


# ---- Errors -------------------------------------------------------------


def test_unknown_tool_returns_jsonrpc_error(mcp_root: Path):
    client = _client(mcp_root)
    resp = _rpc(client, "tools/call", {"name": "no-such", "arguments": {}})
    body = resp.json()
    assert "error" in body
    assert body["error"]["code"] == -32601


def test_unknown_method_returns_jsonrpc_error(mcp_root: Path):
    client = _client(mcp_root)
    resp = _rpc(client, "wat")
    body = resp.json()
    assert body["error"]["code"] == -32601


def test_missing_required_argument_surfaces_as_tool_error(mcp_root: Path):
    client = _client(mcp_root)
    resp = _rpc(client, "tools/call", {
        "name": "get_machine", "arguments": {},
    })
    result = resp.json()["result"]
    assert result["isError"] is True


# ---- Batch --------------------------------------------------------------


def test_batch_request_returns_batch_response(mcp_root: Path):
    client = _client(mcp_root)
    body = [
        {"jsonrpc": "2.0", "id": 1, "method": "ping"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ]
    resp = client.post("/mcp", json=body, headers=_bearer())
    assert resp.status_code == 200
    results = resp.json()
    assert isinstance(results, list) and len(results) == 2
    ids = sorted(r["id"] for r in results)
    assert ids == [1, 2]
