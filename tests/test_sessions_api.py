"""Tests for the /sessions mint endpoint.

This endpoint is the openschool↔tftpjail wire: tftpjail asks openschool to
mint a per-boot session token bound to a MAC, then stamps it into the
rendered grub.cfg. The auth model is a shared secret (Bearer) because tftpjail
is the only legitimate caller — any random host on the network must not be
able to forge boot sessions.
"""

from fastapi.testclient import TestClient

from openschool.server.app import create_app
from openschool.server.boot_sessions import BootSessionStore


SECRET = "shared-secret-from-config"


def _client_with_minting() -> tuple[TestClient, BootSessionStore]:
    store = BootSessionStore()
    app = create_app(sessions=store, mint_secret=SECRET)
    return TestClient(app), store


def test_mint_with_correct_secret_returns_a_session():
    client, store = _client_with_minting()
    response = client.post(
        "/sessions",
        json={"mac": "aa:bb:cc:dd:ee:ff"},
        headers={"Authorization": f"Bearer {SECRET}"},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["mac"] == "aa:bb:cc:dd:ee:ff"
    assert len(body["token"]) >= 64
    # The minted session is now in the store and usable for /status.
    assert store.lookup(body["token"]) is not None


def test_mint_normalises_mac_format():
    client, _store = _client_with_minting()
    response = client.post(
        "/sessions",
        json={"mac": "AA-BB-CC-DD-EE-FF"},
        headers={"Authorization": f"Bearer {SECRET}"},
    )
    assert response.status_code == 201
    assert response.json()["mac"] == "aa:bb:cc:dd:ee:ff"


def test_mint_with_wrong_secret_returns_401_uniformly():
    client, _store = _client_with_minting()
    response = client.post(
        "/sessions",
        json={"mac": "aa:bb:cc:dd:ee:ff"},
        headers={"Authorization": "Bearer wrong-secret"},
    )
    assert response.status_code == 401
    assert response.json() == {"detail": "unauthorised"}


def test_mint_without_auth_header_returns_401_uniformly():
    client, _store = _client_with_minting()
    response = client.post("/sessions", json={"mac": "aa:bb:cc:dd:ee:ff"})
    assert response.status_code == 401


def test_mint_with_minting_disabled_returns_503():
    """When no mint_secret is configured, the endpoint is administratively off."""
    app = create_app(sessions=BootSessionStore(), mint_secret=None)
    client = TestClient(app)
    response = client.post(
        "/sessions",
        json={"mac": "aa:bb:cc:dd:ee:ff"},
        headers={"Authorization": "Bearer anything"},
    )
    assert response.status_code == 503


def test_mint_then_post_status_with_returned_token():
    """End-to-end: mint a token, then use it on /status — the natural lifecycle."""
    client, _store = _client_with_minting()
    mint_response = client.post(
        "/sessions",
        json={"mac": "aa:bb:cc:dd:ee:ff"},
        headers={"Authorization": f"Bearer {SECRET}"},
    )
    token = mint_response.json()["token"]
    status_response = client.post(
        "/status",
        json={"state": "network_up"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert status_response.status_code == 200
