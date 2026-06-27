"""Tests for the FastAPI /status endpoint, exercised via the TestClient."""

from fastapi.testclient import TestClient

from openschool.boot_states import BootState
from openschool.server.app import create_app
from openschool.server.boot_sessions import BootSessionStore


def _client_and_store():
    store = BootSessionStore()
    app = create_app(sessions=store)
    return TestClient(app), store


def test_valid_report_records_state():
    client, store = _client_and_store()
    session = store.mint("aa:bb:cc:dd:ee:ff")
    response = client.post(
        "/status",
        json={"state": "network_up"},
        headers={"Authorization": f"Bearer {session.token}"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body == {"ok": True, "mac": "aa:bb:cc:dd:ee:ff", "state": "network_up"}
    refreshed = store.lookup(session.token)
    assert refreshed is not None
    assert refreshed.latest_state == BootState.NETWORK_UP


def test_user_logged_in_detail_is_recorded():
    client, store = _client_and_store()
    session = store.mint("aa:bb:cc:dd:ee:ff")
    for state in ("network_up", "nfs_mounted", "login_ready"):
        client.post(
            "/status",
            json={"state": state},
            headers={"Authorization": f"Bearer {session.token}"},
        )
    response = client.post(
        "/status",
        json={"state": "user_logged_in", "detail": "alice"},
        headers={"Authorization": f"Bearer {session.token}"},
    )
    assert response.status_code == 200
    assert response.json()["state"] == "user_logged_in"


def test_unknown_token_returns_401_uniformly():
    client, _store = _client_and_store()
    response = client.post(
        "/status",
        json={"state": "network_up"},
        headers={"Authorization": "Bearer not-a-real-token"},
    )
    assert response.status_code == 401
    assert response.json() == {"detail": "unauthorised"}


def test_missing_authorization_header_returns_401_uniformly():
    """Missing and unknown token must look identical to a prober."""
    client, _store = _client_and_store()
    response = client.post("/status", json={"state": "network_up"})
    assert response.status_code == 401
    assert response.json() == {"detail": "unauthorised"}


def test_malformed_authorization_header_returns_401_uniformly():
    client, _store = _client_and_store()
    response = client.post(
        "/status",
        json={"state": "network_up"},
        headers={"Authorization": "Basic something"},
    )
    assert response.status_code == 401
    assert response.json() == {"detail": "unauthorised"}


def test_out_of_order_report_returns_409():
    client, store = _client_and_store()
    session = store.mint("aa:bb:cc:dd:ee:ff")
    client.post(
        "/status",
        json={"state": "login_ready"},
        headers={"Authorization": f"Bearer {session.token}"},
    )
    response = client.post(
        "/status",
        json={"state": "network_up"},
        headers={"Authorization": f"Bearer {session.token}"},
    )
    assert response.status_code == 409


def test_unknown_state_string_returns_422():
    client, store = _client_and_store()
    session = store.mint("aa:bb:cc:dd:ee:ff")
    response = client.post(
        "/status",
        json={"state": "rooted_the_box"},
        headers={"Authorization": f"Bearer {session.token}"},
    )
    assert response.status_code == 422
