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
