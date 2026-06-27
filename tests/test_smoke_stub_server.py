"""Tests for the smoke-test stub server (FastAPI app + static squashfs route)."""

from fastapi.testclient import TestClient

from fleetboot.boot_states import BootState
from fleetboot.server.boot_sessions import BootSessionStore
from tests.smoke.stub_server import (
    SQUASHFS_URL_PATH,
    build_smoke_app,
    find_free_port,
)


def test_find_free_port_returns_a_usable_port():
    port = find_free_port()
    assert 1024 < port < 65536


def test_stub_serves_squashfs_at_known_url(tmp_path):
    payload = b"this stands in for a real squashfs"
    fake_squashfs = tmp_path / "fleetboot.squashfs"
    fake_squashfs.write_bytes(payload)

    sessions = BootSessionStore()
    session = sessions.mint("aa:bb:cc:dd:ee:ff")
    import threading

    event = threading.Event()
    app = build_smoke_app(
        sessions=sessions,
        squashfs_path=fake_squashfs,
        network_up_event=event,
        expected_token=session.token,
    )
    client = TestClient(app)
    response = client.get(SQUASHFS_URL_PATH)
    assert response.status_code == 200
    assert response.content == payload


def test_stub_fires_network_up_event_on_correct_token(tmp_path):
    sessions = BootSessionStore()
    session = sessions.mint("aa:bb:cc:dd:ee:ff")
    import threading

    event = threading.Event()
    app = build_smoke_app(
        sessions=sessions,
        squashfs_path=tmp_path / "missing.squashfs",
        network_up_event=event,
        expected_token=session.token,
    )
    client = TestClient(app)
    response = client.post(
        "/status",
        json={"state": "network_up"},
        headers={"Authorization": f"Bearer {session.token}"},
    )
    assert response.status_code == 200
    assert event.is_set()


def test_stub_ignores_status_posts_from_wrong_token(tmp_path):
    """A status post for an unrelated session must NOT fire our event."""
    sessions = BootSessionStore()
    our = sessions.mint("aa:bb:cc:dd:ee:01")
    other = sessions.mint("aa:bb:cc:dd:ee:02")
    import threading

    event = threading.Event()
    app = build_smoke_app(
        sessions=sessions,
        squashfs_path=tmp_path / "missing.squashfs",
        network_up_event=event,
        expected_token=our.token,
    )
    client = TestClient(app)
    response = client.post(
        "/status",
        json={"state": "network_up"},
        headers={"Authorization": f"Bearer {other.token}"},
    )
    assert response.status_code == 200
    assert not event.is_set()


def test_stub_does_not_fire_event_on_unauthorised_post(tmp_path):
    sessions = BootSessionStore()
    session = sessions.mint("aa:bb:cc:dd:ee:ff")
    import threading

    event = threading.Event()
    app = build_smoke_app(
        sessions=sessions,
        squashfs_path=tmp_path / "missing.squashfs",
        network_up_event=event,
        expected_token=session.token,
    )
    client = TestClient(app)
    response = client.post(
        "/status",
        json={"state": "network_up"},
        headers={"Authorization": "Bearer wrong-token"},
    )
    assert response.status_code == 401
    assert not event.is_set()


def test_session_minting_visible_via_latest_state(tmp_path):
    """The stub exposes the session's progression so the orchestrator can
    report which state was reached on partial successes."""
    sessions = BootSessionStore()
    session = sessions.mint("aa:bb:cc:dd:ee:ff")
    assert sessions.lookup(session.token).latest_state is None
    sessions.record_state(session.token, BootState.NETWORK_UP)
    assert sessions.lookup(session.token).latest_state == BootState.NETWORK_UP
