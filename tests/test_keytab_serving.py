"""Tests for the per-MAC FreeIPA keytab delivery endpoint."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from fleetboot.server.app import create_app
from fleetboot.server.boot_sessions import BootSessionStore


@pytest.fixture
def keytabs_dir(tmp_path: Path) -> Path:
    d = tmp_path / "keytabs"
    d.mkdir()
    return d


def _client(keytabs_dir: Path) -> tuple[TestClient, BootSessionStore]:
    store = BootSessionStore()
    app = create_app(sessions=store, keytabs_dir=keytabs_dir)
    return TestClient(app), store


def test_keytab_served_for_valid_token_and_provisioned_mac(keytabs_dir: Path):
    client, store = _client(keytabs_dir)
    (keytabs_dir / "aa:bb:cc:dd:ee:ff.keytab").write_bytes(b"KEYTAB-BYTES")
    session = store.mint("aa:bb:cc:dd:ee:ff")
    response = client.get(f"/enrol/{session.token}/keytab")
    assert response.status_code == 200
    assert response.content == b"KEYTAB-BYTES"


def test_keytab_returns_404_when_no_keytab_provisioned(keytabs_dir: Path):
    """A registered MAC without a keytab on disk gets 404, not 500."""
    client, store = _client(keytabs_dir)
    session = store.mint("aa:bb:cc:dd:ee:ff")
    response = client.get(f"/enrol/{session.token}/keytab")
    assert response.status_code == 404


def test_keytab_returns_401_for_unknown_token(keytabs_dir: Path):
    """Unknown token (not minted, expired, fabricated) -> 401."""
    client, _store = _client(keytabs_dir)
    response = client.get("/enrol/not-a-real-token/keytab")
    assert response.status_code == 401


def test_keytab_endpoint_disabled_without_dir():
    """create_app(keytabs_dir=None) makes the route return 503."""
    app = create_app(sessions=BootSessionStore(), keytabs_dir=None)
    client = TestClient(app)
    response = client.get("/enrol/anything/keytab")
    assert response.status_code == 503


def test_token_for_one_mac_cannot_fetch_anothers_keytab(keytabs_dir: Path):
    """A session for MAC A must never see MAC B's keytab — even though A's
    token is otherwise valid."""
    client, store = _client(keytabs_dir)
    (keytabs_dir / "aa:bb:cc:dd:ee:01.keytab").write_bytes(b"alice-keytab")
    (keytabs_dir / "aa:bb:cc:dd:ee:02.keytab").write_bytes(b"bob-keytab")
    session_for_alice = store.mint("aa:bb:cc:dd:ee:01")
    response = client.get(f"/enrol/{session_for_alice.token}/keytab")
    assert response.status_code == 200
    # The token is bound to alice's MAC, so the file we get back is
    # alice's, never bob's.
    assert response.content == b"alice-keytab"
