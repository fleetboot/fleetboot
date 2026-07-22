"""Tests for changing an enrolled machine's profile.

Available three ways — dashboard form, admin REST API, MCP tool — and
all three go through `MachineRegistry.set_profile`. The property that
matters everywhere: it changes the profile and *only* the profile.
Re-enrolling replaces the row; this doesn't.
"""

from __future__ import annotations

import base64
import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from fleetboot.server.app import create_app
from fleetboot.server.boot_sessions import BootSessionStore
from fleetboot.server.registry import MachineRegistry


ADMIN = "the-admin-secret"
MAC = "aa:bb:cc:dd:ee:01"


@pytest.fixture
def repo_root(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    profiles = root / "image" / "profiles"
    profiles.mkdir(parents=True)
    for name in ("default", "render-node", "cinnamon-desktop"):
        (profiles / name).mkdir()
    (root / "Makefile").write_text(".PHONY: image\nimage:\n\techo built\n")
    return root


@pytest.fixture
def registry(tmp_path: Path) -> MachineRegistry:
    reg = MachineRegistry(tmp_path / "machines.sqlite")
    reg.enroll(
        mac=MAC, profile_name="default",
        architecture="arm64", platform="pc",
        serial_console=True, scratch_mode="persistent",
    )
    return reg


@pytest.fixture
def client(repo_root: Path, registry: MachineRegistry) -> TestClient:
    app = create_app(
        sessions=BootSessionStore(),
        registry=registry,
        admin_secret=ADMIN,
        dashboard_repo_root=repo_root,
    )
    return TestClient(app)


def _basic_auth() -> dict[str, str]:
    token = base64.b64encode(f"admin:{ADMIN}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def _bearer() -> dict[str, str]:
    return {"Authorization": f"Bearer {ADMIN}"}


def _call_tool(client: TestClient, name: str, arguments: dict) -> dict:
    response = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
        headers=_bearer(),
    )
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    return {
        "payload": json.loads(result["content"][0]["text"]),
        "is_error": result["isError"],
    }


# ---- Registry -----------------------------------------------------------


def test_set_profile_changes_only_the_profile(registry: MachineRegistry):
    """Everything else on the row is deliberate configuration — losing
    it because someone reclassified a machine would be a nasty
    surprise, especially the power wiring."""
    registry.set_pdu_alias(MAC, "rack1-port7")
    registry.set_power_command(MAC, "on", "switch-on.sh")
    before = registry.lookup(MAC)

    assert registry.set_profile(MAC, "render-node") is True

    after = registry.lookup(MAC)
    assert after.profile_name == "render-node"
    assert after.architecture == before.architecture == "arm64"
    assert after.platform == before.platform == "pc"
    assert after.serial_console is True
    assert after.scratch_mode == "persistent"
    assert after.pdu_alias == "rack1-port7"
    assert after.power_on_command == "switch-on.sh"
    assert after.created_at == before.created_at
    assert after.enrolled_by == before.enrolled_by


def test_set_profile_reports_an_unknown_mac(registry: MachineRegistry):
    """False, not an exception — callers turn it into a 404."""
    assert registry.set_profile("ff:ff:ff:ff:ff:ff", "render-node") is False


def test_set_profile_rejects_an_empty_name(registry: MachineRegistry):
    with pytest.raises(ValueError, match="must not be empty"):
        registry.set_profile(MAC, "   ")


def test_set_profile_does_not_disturb_a_pending_reboot(
    registry: MachineRegistry,
):
    """Changing the profile is not itself a reboot, and must not cancel
    one that was already armed."""
    registry.set_pending_reboot(MAC, True)
    registry.set_profile(MAC, "render-node")
    assert registry.lookup(MAC).pending_reboot is True


# ---- Admin REST API -----------------------------------------------------


def test_patch_machines_changes_the_profile(client: TestClient):
    response = client.patch(
        f"/machines/{MAC}",
        json={"profile_name": "render-node"},
        headers=_bearer(),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["profile_name"] == "render-node"
    # Untouched fields come back as they were.
    assert body["architecture"] == "arm64"
    assert body["scratch_mode"] == "persistent"


def test_patch_machines_404s_for_an_unknown_mac(client: TestClient):
    response = client.patch(
        "/machines/ff:ff:ff:ff:ff:ff",
        json={"profile_name": "render-node"},
        headers=_bearer(),
    )
    assert response.status_code == 404


def test_patch_machines_requires_the_admin_secret(client: TestClient):
    response = client.patch(
        f"/machines/{MAC}", json={"profile_name": "render-node"},
    )
    assert response.status_code == 401
    # And the row is untouched.
    assert client.get(
        f"/machines/{MAC}", headers=_bearer(),
    ).json()["profile_name"] == "default"


def test_patch_machines_rejects_an_empty_profile(client: TestClient):
    response = client.patch(
        f"/machines/{MAC}", json={"profile_name": ""}, headers=_bearer(),
    )
    assert response.status_code == 422


# ---- Dashboard ----------------------------------------------------------


def test_dashboard_form_changes_the_profile(
    client: TestClient, registry: MachineRegistry,
):
    response = client.post(
        f"/dashboard/machines/{MAC}/profile",
        data={"profile_name": "cinnamon-desktop"},
        headers=_basic_auth(),
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert registry.lookup(MAC).profile_name == "cinnamon-desktop"


def test_dashboard_rejects_a_profile_that_does_not_exist(
    client: TestClient, registry: MachineRegistry,
):
    """The dropdown only offers real profiles, but a hand-rolled POST
    could enrol a machine onto a profile with no image — which fails
    later, at boot, on the machine."""
    response = client.post(
        f"/dashboard/machines/{MAC}/profile",
        data={"profile_name": "no-such-profile"},
        headers=_basic_auth(),
    )

    assert response.status_code == 400
    assert registry.lookup(MAC).profile_name == "default"


def test_machine_page_offers_every_profile_with_the_current_one_selected(
    client: TestClient,
):
    page = client.get(
        f"/dashboard/machines/{MAC}", headers=_basic_auth(),
    ).text

    assert f'action="/dashboard/machines/{MAC}/profile"' in page
    for name in ("default", "render-node", "cinnamon-desktop"):
        assert f'value="{name}"' in page
    # The machine's current profile is the pre-selected option, so the
    # form doesn't quietly propose a change nobody asked for.
    assert re.search(r'value="default"\s+selected', page)
    assert not re.search(r'value="render-node"\s+selected', page)


# ---- MCP ----------------------------------------------------------------


def test_mcp_tool_changes_the_profile(
    client: TestClient, registry: MachineRegistry,
):
    got = _call_tool(client, "set_machine_profile", {
        "mac": MAC, "profile_name": "render-node",
    })

    assert got["is_error"] is False
    assert got["payload"]["profile_name"] == "render-node"
    assert got["payload"]["architecture"] == "arm64"
    assert registry.lookup(MAC).profile_name == "render-node"


def test_mcp_tool_rejects_an_unknown_profile(
    client: TestClient, registry: MachineRegistry,
):
    got = _call_tool(client, "set_machine_profile", {
        "mac": MAC, "profile_name": "no-such-profile",
    })

    assert got["is_error"] is True
    message = got["payload"]["error"]
    assert "unknown profile" in message
    # The error teaches: it names what was available.
    assert "render-node" in message
    assert registry.lookup(MAC).profile_name == "default"


def test_mcp_tool_rejects_an_unknown_mac(client: TestClient):
    got = _call_tool(client, "set_machine_profile", {
        "mac": "ff:ff:ff:ff:ff:ff", "profile_name": "render-node",
    })
    assert got["is_error"] is True
    assert "no machine" in got["payload"]["error"]


def test_mcp_tool_is_advertised_with_both_arguments_required(
    client: TestClient,
):
    response = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers=_bearer(),
    )
    tools = {t["name"]: t for t in response.json()["result"]["tools"]}

    assert "set_machine_profile" in tools
    schema = tools["set_machine_profile"]["inputSchema"]
    assert set(schema["required"]) == {"mac", "profile_name"}
