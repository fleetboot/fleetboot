"""Tests for the tftpjail-facing /resolve/{mac} endpoint."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from fleetboot.server.app import create_app
from fleetboot.server.boot_sessions import BootSessionStore
from fleetboot.server.registry import MachineRegistry


MINT_SECRET = "tftpjail-shared-secret"


@pytest.fixture
def app_with_registry(tmp_path: Path):
    registry = MachineRegistry(tmp_path / "m.sqlite")
    registry.enroll(
        mac="aa:bb:cc:dd:ee:ff",
        profile_name="lab",
        architecture="x86_64",
        platform="efi",
    )
    app = create_app(
        sessions=BootSessionStore(),
        mint_secret=MINT_SECRET,
        registry=registry,
    )
    return TestClient(app)


def test_resolve_known_mac_returns_record(app_with_registry):
    response = app_with_registry.get(
        "/resolve/aa:bb:cc:dd:ee:ff",
        headers={"Authorization": f"Bearer {MINT_SECRET}"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["mac"] == "aa:bb:cc:dd:ee:ff"
    assert body["profile_name"] == "lab"
    assert body["architecture"] == "x86_64"


def test_resolve_unknown_mac_returns_404(app_with_registry):
    response = app_with_registry.get(
        "/resolve/aa:bb:cc:dd:ee:00",
        headers={"Authorization": f"Bearer {MINT_SECRET}"},
    )
    assert response.status_code == 404


def test_resolve_without_secret_returns_401(app_with_registry):
    response = app_with_registry.get("/resolve/aa:bb:cc:dd:ee:ff")
    assert response.status_code == 401


def test_resolve_with_wrong_secret_returns_401(app_with_registry):
    response = app_with_registry.get(
        "/resolve/aa:bb:cc:dd:ee:ff",
        headers={"Authorization": "Bearer wrong"},
    )
    assert response.status_code == 401


def test_resolve_returns_503_without_registry():
    app = create_app(
        sessions=BootSessionStore(),
        mint_secret=MINT_SECRET,
        registry=None,
    )
    client = TestClient(app)
    response = client.get(
        "/resolve/aa:bb:cc:dd:ee:ff",
        headers={"Authorization": f"Bearer {MINT_SECRET}"},
    )
    assert response.status_code == 503
