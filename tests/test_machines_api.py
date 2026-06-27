"""Tests for the /machines admin API.

Same auth pattern as /sessions (shared-secret Bearer), but with a different
secret. Admins enrol/delete machines; tftpjail only reads (via the existing
mint flow, plus a future read endpoint).
"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from fleetboot.server.app import create_app
from fleetboot.server.boot_sessions import BootSessionStore
from fleetboot.server.registry import MachineRegistry


ADMIN_SECRET = "the-admin-shared-secret"


@pytest.fixture
def client_and_registry(tmp_path: Path):
    registry = MachineRegistry(tmp_path / "machines.sqlite")
    app = create_app(
        sessions=BootSessionStore(),
        registry=registry,
        admin_secret=ADMIN_SECRET,
    )
    return TestClient(app), registry


def _admin_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {ADMIN_SECRET}"}


def test_enroll_round_trips(client_and_registry):
    client, registry = client_and_registry
    response = client.post(
        "/machines",
        json={
            "mac": "aa:bb:cc:dd:ee:ff",
            "profile_name": "student-lab",
            "architecture": "x86_64",
            "platform": "efi",
        },
        headers=_admin_headers(),
    )
    assert response.status_code == 201
    body = response.json()
    assert body["mac"] == "aa:bb:cc:dd:ee:ff"
    assert body["profile_name"] == "student-lab"
    assert registry.lookup("aa:bb:cc:dd:ee:ff") is not None


def test_enroll_normalises_mac(client_and_registry):
    client, _ = client_and_registry
    response = client.post(
        "/machines",
        json={
            "mac": "AA-BB-CC-DD-EE-FF",
            "profile_name": "p",
            "architecture": "x86_64",
            "platform": "efi",
        },
        headers=_admin_headers(),
    )
    assert response.json()["mac"] == "aa:bb:cc:dd:ee:ff"


def test_enroll_without_secret_returns_401(client_and_registry):
    client, _ = client_and_registry
    response = client.post(
        "/machines",
        json={
            "mac": "aa:bb:cc:dd:ee:ff",
            "profile_name": "p",
            "architecture": "x86_64",
            "platform": "efi",
        },
    )
    assert response.status_code == 401


def test_enroll_with_wrong_secret_returns_401(client_and_registry):
    client, _ = client_and_registry
    response = client.post(
        "/machines",
        json={
            "mac": "aa:bb:cc:dd:ee:ff",
            "profile_name": "p",
            "architecture": "x86_64",
            "platform": "efi",
        },
        headers={"Authorization": "Bearer wrong"},
    )
    assert response.status_code == 401


def test_list_machines(client_and_registry):
    client, _ = client_and_registry
    for last_octet in ("01", "02"):
        client.post(
            "/machines",
            json={
                "mac": f"aa:bb:cc:dd:ee:{last_octet}",
                "profile_name": "p",
                "architecture": "x86_64",
                "platform": "efi",
            },
            headers=_admin_headers(),
        )
    response = client.get("/machines", headers=_admin_headers())
    assert response.status_code == 200
    macs = [row["mac"] for row in response.json()]
    assert macs == ["aa:bb:cc:dd:ee:01", "aa:bb:cc:dd:ee:02"]


def test_get_single_machine(client_and_registry):
    client, _ = client_and_registry
    client.post(
        "/machines",
        json={
            "mac": "aa:bb:cc:dd:ee:ff",
            "profile_name": "p",
            "architecture": "x86_64",
            "platform": "efi",
        },
        headers=_admin_headers(),
    )
    response = client.get(
        "/machines/aa:bb:cc:dd:ee:ff", headers=_admin_headers()
    )
    assert response.status_code == 200
    assert response.json()["mac"] == "aa:bb:cc:dd:ee:ff"


def test_get_unknown_machine_returns_404(client_and_registry):
    client, _ = client_and_registry
    response = client.get(
        "/machines/aa:bb:cc:dd:ee:00", headers=_admin_headers()
    )
    assert response.status_code == 404


def test_delete_existing_returns_204(client_and_registry):
    client, registry = client_and_registry
    client.post(
        "/machines",
        json={
            "mac": "aa:bb:cc:dd:ee:ff",
            "profile_name": "p",
            "architecture": "x86_64",
            "platform": "efi",
        },
        headers=_admin_headers(),
    )
    response = client.delete(
        "/machines/aa:bb:cc:dd:ee:ff", headers=_admin_headers()
    )
    assert response.status_code == 204
    assert registry.lookup("aa:bb:cc:dd:ee:ff") is None


def test_delete_unknown_returns_404(client_and_registry):
    client, _ = client_and_registry
    response = client.delete(
        "/machines/aa:bb:cc:dd:ee:ff", headers=_admin_headers()
    )
    assert response.status_code == 404


def test_registry_disabled_when_no_admin_secret(tmp_path: Path):
    """Even with a registry attached, /machines is off until a secret is set."""
    app = create_app(
        sessions=BootSessionStore(),
        registry=MachineRegistry(tmp_path / "m.sqlite"),
        admin_secret=None,
    )
    client = TestClient(app)
    response = client.get(
        "/machines", headers={"Authorization": "Bearer anything"}
    )
    assert response.status_code == 503


def test_registry_disabled_when_no_registry():
    """And likewise with a secret but no registry."""
    app = create_app(
        sessions=BootSessionStore(),
        registry=None,
        admin_secret="anything",
    )
    client = TestClient(app)
    response = client.get(
        "/machines", headers={"Authorization": "Bearer anything"}
    )
    assert response.status_code == 503
