"""Tests for the FastAPI /status endpoint, exercised via the TestClient."""

from fastapi.testclient import TestClient

from fleetboot.boot_states import BootState
from fleetboot.server.app import create_app
from fleetboot.server.boot_sessions import BootSessionStore


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


def test_hostname_field_is_persisted_on_registered_machine(tmp_path):
    """A hostname in the body lands on the machines.hostname column."""
    from pathlib import Path
    from fleetboot.server.registry import MachineRegistry

    store = BootSessionStore()
    registry = MachineRegistry(Path(tmp_path) / "machines.sqlite")
    registry.enroll(
        mac="aa:bb:cc:dd:ee:ff", profile_name="default",
        architecture="x86_64", platform="efi",
    )
    app = create_app(sessions=store, registry=registry)
    client = TestClient(app)
    session = store.mint("aa:bb:cc:dd:ee:ff")
    response = client.post(
        "/status",
        json={"state": "network_up", "hostname": "lab-pc-01"},
        headers={"Authorization": f"Bearer {session.token}"},
    )
    assert response.status_code == 200
    machine = registry.lookup("aa:bb:cc:dd:ee:ff")
    assert machine is not None
    assert machine.hostname == "lab-pc-01"


def test_grub_event_records_boot_event(tmp_path):
    """GET /grub-event/<token>/<state> from grub records as a boot
    event, validated against the token + state index."""
    from pathlib import Path
    from fleetboot.server.registry import MachineRegistry

    store = BootSessionStore()
    registry = MachineRegistry(Path(tmp_path) / "machines.sqlite")
    registry.enroll(
        mac="aa:bb:cc:dd:ee:ff", profile_name="default",
        architecture="x86_64", platform="efi",
    )
    app = create_app(sessions=store, registry=registry)
    client = TestClient(app)
    session = store.mint("aa:bb:cc:dd:ee:ff")

    response = client.get(f"/grub-event/{session.token}/grub_running")
    assert response.status_code == 200
    # Empty body + Connection: close. Content beyond 0 bytes triggers
    # a 30s stall on the OptiPlex's BIOS PXE TCP stack (FIN propagation).
    assert response.content == b""
    assert response.headers.get("connection") == "close"

    events = registry.recent_boot_events(mac="aa:bb:cc:dd:ee:ff")
    assert any(e.state == "grub_running" for e in events)


def test_grub_event_rejects_unknown_state(tmp_path):
    from pathlib import Path
    from fleetboot.server.registry import MachineRegistry

    store = BootSessionStore()
    registry = MachineRegistry(Path(tmp_path) / "machines.sqlite")
    app = create_app(sessions=store, registry=registry)
    client = TestClient(app)
    session = store.mint("aa:bb:cc:dd:ee:ff")
    response = client.get(f"/grub-event/{session.token}/nonsense")
    assert response.status_code == 400


def test_grub_event_rejects_unknown_token(tmp_path):
    from pathlib import Path
    from fleetboot.server.registry import MachineRegistry

    store = BootSessionStore()
    registry = MachineRegistry(Path(tmp_path) / "machines.sqlite")
    app = create_app(sessions=store, registry=registry)
    client = TestClient(app)
    response = client.get("/grub-event/not-a-token/grub_running")
    assert response.status_code == 401


def test_consecutive_same_state_dedups_event_rows(tmp_path):
    """The heartbeat re-reports the same state every 2 min; the events
    table should not grow without bound."""
    from pathlib import Path
    from fleetboot.boot_states import BootState
    from fleetboot.server.registry import MachineRegistry

    store = BootSessionStore()
    registry = MachineRegistry(Path(tmp_path) / "machines.sqlite")
    registry.enroll(
        mac="aa:bb:cc:dd:ee:ff", profile_name="default",
        architecture="x86_64", platform="efi",
    )
    app = create_app(sessions=store, registry=registry)
    client = TestClient(app)
    session = store.mint("aa:bb:cc:dd:ee:ff")
    # Three consecutive same-state reports.
    for _ in range(3):
        client.post(
            "/status",
            json={"state": "network_up"},
            headers={"Authorization": f"Bearer {session.token}"},
        )
    events = registry.recent_boot_events(mac="aa:bb:cc:dd:ee:ff")
    # Exactly one event row for the three identical reports.
    assert len([e for e in events if e.state == "network_up"]) == 1


def test_diagnostics_field_lands_on_machine_row(tmp_path):
    """A /status post with a diagnostics body overwrites the latest
    machine.last_diagnostics."""
    from pathlib import Path
    from fleetboot.server.registry import MachineRegistry

    store = BootSessionStore()
    registry = MachineRegistry(Path(tmp_path) / "machines.sqlite")
    registry.enroll(
        mac="aa:bb:cc:dd:ee:ff", profile_name="default",
        architecture="x86_64", platform="efi",
    )
    app = create_app(sessions=store, registry=registry)
    client = TestClient(app)
    session = store.mint("aa:bb:cc:dd:ee:ff")
    response = client.post(
        "/status",
        json={
            "state": "network_up",
            "diagnostics": "# systemctl --failed\nlightdm.service\n",
        },
        headers={"Authorization": f"Bearer {session.token}"},
    )
    assert response.status_code == 200
    m = registry.lookup("aa:bb:cc:dd:ee:ff")
    assert m is not None
    assert m.last_diagnostics is not None
    assert "lightdm.service" in m.last_diagnostics


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
