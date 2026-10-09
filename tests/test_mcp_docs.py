"""Tests for fleetboot's self-documentation over MCP.

Two surfaces carry the same pages — the `describe_fleetboot` tool and
MCP resources — so most of these tests check that they agree, and that
the prose doesn't drift away from the tool catalogue it describes.
"""

from __future__ import annotations

import base64
import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from fleetboot.server import mcp_docs
from fleetboot.server.app import create_app
from fleetboot.server.boot_sessions import BootSessionStore
from fleetboot.server.mcp import _tool_catalogue
from fleetboot.server.registry import MachineRegistry


ADMIN = "the-admin-secret"


@pytest.fixture
def docs_root(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    profiles = root / "image" / "profiles"
    profiles.mkdir(parents=True)
    (profiles / "default").mkdir()
    (root / "Makefile").write_text(".PHONY: image\nimage:\n\techo built\n")
    return root


def _client(docs_root: Path) -> TestClient:
    app = create_app(
        sessions=BootSessionStore(),
        registry=MachineRegistry(docs_root / "machines.sqlite"),
        admin_secret=ADMIN,
        dashboard_repo_root=docs_root,
    )
    return TestClient(app)


def _rpc(client: TestClient, method: str, params: dict | None = None,
         *, id_: int = 1) -> dict:
    body: dict = {"jsonrpc": "2.0", "id": id_, "method": method}
    if params is not None:
        body["params"] = params
    resp = client.post(
        "/mcp", json=body, headers={"Authorization": f"Bearer {ADMIN}"},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


# ---- The topic catalogue ------------------------------------------------


def test_every_topic_has_a_name_summary_and_body():
    assert mcp_docs.TOPICS, "there must be at least one documentation topic"
    for topic in mcp_docs.TOPICS:
        assert topic.name and topic.title and topic.summary
        assert len(topic.body) > 500, f"{topic.name} looks like a stub"
        assert topic.body.startswith("# "), (
            f"{topic.name} should open with a markdown heading"
        )


def test_topic_names_are_unique_and_url_safe():
    names = mcp_docs.topic_names()
    assert len(names) == len(set(names))
    for name in names:
        assert re.fullmatch(r"[a-z][a-z0-9-]*", name), name


def test_default_topic_exists():
    assert mcp_docs.get_topic(mcp_docs.DEFAULT_TOPIC) is not None


def test_topic_uri_round_trips():
    for topic in mcp_docs.TOPICS:
        assert topic.uri.startswith(mcp_docs.DOC_URI_PREFIX)
        assert mcp_docs.topic_for_uri(topic.uri) is topic
    assert mcp_docs.topic_for_uri("fleetboot://docs/nope") is None
    assert mcp_docs.topic_for_uri("https://example.org/") is None


def test_topic_index_lists_every_topic():
    index = mcp_docs.topic_index()
    for topic in mcp_docs.TOPICS:
        assert f"`{topic.name}`" in index


# ---- On-demand compute norms: don't leave workers running ---------------


def test_on_demand_doc_tells_agents_to_power_down_when_done():
    """An agent using the fleet as compute must be told, in the docs it
    reads, to switch workers off when the work finishes — otherwise the
    obvious failure mode is a pool left running idle, burning power."""
    topic = mcp_docs.get_topic("on-demand-compute")
    assert topic is not None
    body = topic.body.lower()
    assert "responsibility" in body, (
        "the on-demand doc should spell out the agent's power responsibility"
    )
    # It must say both halves of the discipline: on for work, off when done.
    assert "off the moment the work is done" in body
    assert "idle" in body


def test_server_instructions_tell_agents_to_power_down_when_idle():
    """The always-on server instructions (not just the deep-dive topic)
    must carry the power-down norm, since an agent sees them first."""
    from fleetboot.server.mcp import SERVER_INSTRUCTIONS

    text = SERVER_INSTRUCTIONS.lower()
    assert "own the power" in text or "power bill" in text
    assert "idle" in text
    assert "claude-compute" in text, (
        "the ready-made worker profile should be advertised in the instructions"
    )


# ---- Framing: generic compute + permanent vs temporary -----------------


def test_docs_frame_fleetboot_as_generic_agent_compute():
    """An agent must be able to learn from the docs that fleetboot gives
    it a temporary machine to run arbitrary compute on — compile a
    kernel, cross-compile, run a job — not merely a fixed 'render pool'."""
    prose = "\n".join(t.body for t in mcp_docs.TOPICS).lower()
    assert "generic compute" in prose
    assert "compile a kernel" in prose
    assert "cross-compile" in prose
    # And that the work happens over SSH on a worker you power down after.
    on_demand = mcp_docs.get_topic("on-demand-compute").body.lower()
    assert "ssh" in on_demand
    assert "claude-compute" in on_demand


def test_docs_cover_both_permanent_and_temporary_deployments():
    """Both shapes must be visible: permanently-running fleets (the
    school use case) and temporary, rent-by-the-job compute."""
    overview = mcp_docs.get_topic("overview").body.lower()
    assert "school" in overview
    assert "permanently-running" in overview
    assert "temporary" in overview


def test_server_instructions_frame_both_use_cases():
    """The always-on instructions must present both jobs: a permanent
    school-style deployment and generic on-demand compute."""
    from fleetboot.server.mcp import SERVER_INSTRUCTIONS

    text = SERVER_INSTRUCTIONS.lower()
    assert "school" in text
    assert "generic compute" in text
    assert "compile a kernel" in text
    assert "cross-compile" in text


def test_on_demand_doc_explains_scratch_for_space_and_persistence():
    """Agents need to know: RAM is the default and is small + wiped, and
    /var/scratch (persistent) gives more space and survives reservations."""
    body = " ".join(
        mcp_docs.get_topic("on-demand-compute").body.lower().split()
    )
    assert "/var/scratch" in body
    assert "tmpfs" in body or "ram" in body
    assert "persistent" in body
    assert "across reservations" in body
    assert "scratch_mode" in body


def test_reservations_topic_explains_availability_and_pool_optin():
    """The dedicated topic must teach the off-is-not-unavailable point,
    the permanent-by-default posture, and the reserve/release loop."""
    topic = mcp_docs.get_topic("reservations")
    assert topic is not None
    body = topic.body.lower()
    assert "available" in body and "permanent" in body and "reserved" in body
    assert "permanent by default" in body or "permanent unless" in body
    assert "release_to_pool" in body
    for tool in ("reserve_machines", "release_machines", "renew_reservation"):
        assert tool in topic.body


def test_docs_say_use_generic_profile_and_install_at_runtime():
    """The agent shouldn't build a bespoke image per task: the docs must
    steer it to the generic worker + install-at-run-time, and reserve a
    dedicated profile for jobs run often."""
    on_demand = mcp_docs.get_topic("on-demand-compute").body.lower()
    assert "per job" in on_demand
    assert "at run time" in on_demand
    assert "claude-compute" in on_demand
    # The "only build your own if you repeat the job" caveat.
    assert "repeat" in on_demand or "often" in on_demand

    from fleetboot.server.mcp import SERVER_INSTRUCTIONS
    instr = SERVER_INSTRUCTIONS.lower()
    assert "run time" in instr
    assert "repeat often" in instr or "repeat" in instr


def test_docs_tell_agents_the_worker_login_needs_no_key():
    """An agent must learn from the docs that a claude-compute worker
    takes a fixed fleetboot/fleetboot login with sudo — no SSH key to
    provision — so it doesn't get stuck trying to inject one."""
    def _flat(topic: str) -> str:
        # Collapse line wrapping so substring checks don't trip on newlines.
        return " ".join(mcp_docs.get_topic(topic).body.lower().split())

    on_demand = _flat("on-demand-compute")
    assert "ssh fleetboot@" in on_demand
    assert "provision an ssh key" in on_demand
    reservations = _flat("reservations")
    assert "no key to provision" in reservations
    assert "ssh fleetboot@" in reservations


def test_docs_tell_agents_how_to_find_a_machine_address():
    """An agent has to reach a machine it powered on. The docs must say
    the address is `last_ip`, and that it is only a last-known value —
    empty before a first boot, stale once the machine is off."""
    def _flat(text: str) -> str:
        return " ".join(text.lower().split())

    services = _flat(mcp_docs.get_topic("services").body)
    assert "`last_ip`" in services
    assert "`last_ip_at`" in services
    assert "never booted" in services
    assert "only while `online` is true" in services
    # The compute walkthrough points at it where the agent needs it.
    assert "`last_ip`" in _flat(mcp_docs.get_topic("on-demand-compute").body)


def test_agents_are_told_to_ask_fleetboot_rather_than_scan_the_network():
    """An agent that can't find its worker tends to reach for a ping
    sweep or port scan. Everywhere it might look first — the server
    instructions, the docs, the tool description — must point it at
    `last_ip` instead."""
    from fleetboot.server.mcp import SERVER_INSTRUCTIONS

    def _flat(text: str) -> str:
        return " ".join(text.lower().split())

    instructions = _flat(SERVER_INSTRUCTIONS)
    assert "don't scan the network" in instructions
    assert "`last_ip`" in instructions

    services = _flat(mcp_docs.get_topic("services").body)
    assert "do not scan the network" in services

    on_demand = _flat(mcp_docs.get_topic("on-demand-compute").body)
    assert "scanning the network" in on_demand


# ---- Anti-drift: the prose vs the real server ---------------------------


def test_every_tool_is_mentioned_in_the_documentation():
    """A tool nobody documents is a tool an agent won't reach for."""
    prose = "\n".join(topic.body for topic in mcp_docs.TOPICS)
    for tool in _tool_catalogue():
        assert f"`{tool['name']}`" in prose, (
            f"{tool['name']} is not mentioned anywhere in the docs"
        )


def test_documented_tool_calls_name_real_tools():
    """The JSON examples must not invoke tools that don't exist."""
    known = {tool["name"] for tool in _tool_catalogue()}
    prose = "\n".join(topic.body for topic in mcp_docs.TOPICS)
    called = set(re.findall(r'"name":\s*"([a-z_]+)"', prose))
    assert called, "the docs should contain worked tool-call examples"
    assert called <= known, f"docs call unknown tools: {called - known}"


def test_documented_boot_states_are_real_states():
    """The lifecycle page must match fleetboot.boot_states."""
    from fleetboot.boot_states import BOOT_STATE_ORDER

    body = mcp_docs.get_topic("boot-lifecycle").body
    for state in BOOT_STATE_ORDER:
        assert f"`{state.value}`" in body, f"{state.value} undocumented"


def test_documented_http_rails_are_real_routes(docs_root: Path):
    """Endpoints named in the services page must exist on the app."""
    routes = {getattr(r, "path", "") for r in _client(docs_root).app.routes}
    body = mcp_docs.get_topic("services").body
    for path in ("/status", "/sessions", "/resolve/{mac}",
                 "/boot/{token}/{filename}", "/enrol/{token}/keytab"):
        assert path in routes, f"{path} is not a real route"
        assert path in body, f"{path} is missing from the services docs"


# ---- The describe_fleetboot tool ----------------------------------------


def test_describe_fleetboot_is_advertised(docs_root: Path):
    client = _client(docs_root)
    tools = _rpc(client, "tools/list")["result"]["tools"]
    by_name = {tool["name"]: tool for tool in tools}
    assert "describe_fleetboot" in by_name
    schema = by_name["describe_fleetboot"]["inputSchema"]
    assert schema["properties"]["topic"]["enum"] == mcp_docs.topic_names()
    # No required arguments: an agent can call it knowing nothing.
    assert not schema.get("required")


def test_describe_fleetboot_returns_markdown_for_every_topic(
    docs_root: Path,
):
    client = _client(docs_root)
    for topic in mcp_docs.TOPICS:
        result = _rpc(client, "tools/call", {
            "name": "describe_fleetboot",
            "arguments": {"topic": topic.name},
        })["result"]
        assert result["isError"] is False
        text = result["content"][0]["text"]
        assert text.startswith(topic.body)
        # Markdown, not a JSON-encoded string.
        with pytest.raises(json.JSONDecodeError):
            json.loads(text)


def test_describe_fleetboot_defaults_to_the_overview(docs_root: Path):
    client = _client(docs_root)
    result = _rpc(client, "tools/call", {
        "name": "describe_fleetboot", "arguments": {},
    })["result"]
    assert mcp_docs.get_topic(mcp_docs.DEFAULT_TOPIC).body in \
        result["content"][0]["text"]


def test_describe_fleetboot_answer_points_at_the_other_topics(
    docs_root: Path,
):
    client = _client(docs_root)
    text = _rpc(client, "tools/call", {
        "name": "describe_fleetboot", "arguments": {"topic": "services"},
    })["result"]["content"][0]["text"]
    for topic in mcp_docs.TOPICS:
        assert f"`{topic.name}`" in text


def test_describe_fleetboot_rejects_an_unknown_topic(docs_root: Path):
    client = _client(docs_root)
    result = _rpc(client, "tools/call", {
        "name": "describe_fleetboot", "arguments": {"topic": "wat"},
    })["result"]
    assert result["isError"] is True
    message = json.loads(result["content"][0]["text"])["error"]
    assert "unknown topic" in message
    # The error teaches: it lists what the agent could have asked for.
    for name in mcp_docs.topic_names():
        assert name in message


# ---- The same pages as MCP resources ------------------------------------


def test_initialize_advertises_the_resources_capability(docs_root: Path):
    client = _client(docs_root)
    result = _rpc(client, "initialize", {})["result"]
    assert "resources" in result["capabilities"]
    # And the briefing tells the model self-documentation exists.
    assert "describe_fleetboot" in result["instructions"]


def test_resources_list_returns_one_entry_per_topic(docs_root: Path):
    client = _client(docs_root)
    resources = _rpc(client, "resources/list")["result"]["resources"]
    assert [r["uri"] for r in resources] == [
        t.uri for t in mcp_docs.TOPICS
    ]
    for resource in resources:
        assert resource["mimeType"] == mcp_docs.DOC_MIME_TYPE
        assert resource["description"]


def test_resources_read_returns_the_topic_body(docs_root: Path):
    client = _client(docs_root)
    for topic in mcp_docs.TOPICS:
        contents = _rpc(client, "resources/read", {"uri": topic.uri})[
            "result"]["contents"]
        assert len(contents) == 1
        assert contents[0]["uri"] == topic.uri
        assert contents[0]["mimeType"] == mcp_docs.DOC_MIME_TYPE
        assert contents[0]["text"].startswith(topic.body)


def test_resource_and_tool_surfaces_serve_identical_text(docs_root: Path):
    """Two doors, one room — a client shouldn't get different docs
    depending on which surface it happens to support."""
    client = _client(docs_root)
    for topic in mcp_docs.TOPICS:
        via_resource = _rpc(client, "resources/read", {"uri": topic.uri})[
            "result"]["contents"][0]["text"]
        via_tool = _rpc(client, "tools/call", {
            "name": "describe_fleetboot",
            "arguments": {"topic": topic.name},
        })["result"]["content"][0]["text"]
        assert via_resource == via_tool


def test_resources_read_rejects_an_unknown_uri(docs_root: Path):
    client = _client(docs_root)
    body = _rpc(client, "resources/read", {"uri": "fleetboot://docs/nope"})
    assert body["error"]["code"] == -32602
    assert "fleetboot://docs/overview" in body["error"]["message"]


def test_resource_templates_list_is_empty_but_answered(docs_root: Path):
    client = _client(docs_root)
    result = _rpc(client, "resources/templates/list")["result"]
    assert result == {"resourceTemplates": []}


def test_resources_require_the_bearer_token(docs_root: Path):
    client = _client(docs_root)
    resp = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "resources/list"},
    )
    assert resp.status_code == 401


# ---- The dashboard renders the same docs --------------------------------


def test_dashboard_mcp_page_shows_every_topic(docs_root: Path):
    client = _client(docs_root)
    credentials = base64.b64encode(f"admin:{ADMIN}".encode()).decode()
    resp = client.get(
        "/dashboard/mcp",
        headers={"Authorization": f"Basic {credentials}"},
    )
    assert resp.status_code == 200
    html = resp.text
    for topic in mcp_docs.TOPICS:
        assert topic.title in html
        assert topic.uri in html
    assert "describe_fleetboot" in html
