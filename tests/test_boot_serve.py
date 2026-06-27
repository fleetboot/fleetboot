"""Tests for /boot/{filename} — serving kernel/initrd/squashfs to GRUB and
live-boot under per-boot session token auth.

The auth model is a query-string token (`?t=`) because bootloaders typically
cannot set HTTP headers when fetching. Validation is identical to /status:
unknown token → 401, no enumeration oracle for which-files-exist.
"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from openschool.server.app import ALLOWED_BOOT_FILES, create_app
from openschool.server.boot_sessions import BootSessionStore


def _setup(tmp_path: Path) -> tuple[TestClient, BootSessionStore, Path]:
    boot_dir = tmp_path / "boot"
    boot_dir.mkdir()
    store = BootSessionStore()
    app = create_app(sessions=store, boot_dir=boot_dir)
    return TestClient(app), store, boot_dir


def test_authorised_request_for_existing_file_serves_bytes(tmp_path: Path):
    client, store, boot_dir = _setup(tmp_path)
    (boot_dir / "vmlinuz").write_bytes(b"\x7fELF...kernel-payload")
    session = store.mint("aa:bb:cc:dd:ee:ff")
    response = client.get(f"/boot/vmlinuz?t={session.token}")
    assert response.status_code == 200
    assert response.content == b"\x7fELF...kernel-payload"


@pytest.mark.parametrize("filename", sorted(ALLOWED_BOOT_FILES))
def test_every_allowlisted_filename_resolves_when_present(
    tmp_path: Path, filename: str
):
    client, store, boot_dir = _setup(tmp_path)
    (boot_dir / filename).write_bytes(b"contents")
    session = store.mint("aa:bb:cc:dd:ee:ff")
    response = client.get(f"/boot/{filename}?t={session.token}")
    assert response.status_code == 200
    assert response.content == b"contents"


def test_unknown_filename_returns_404_without_filesystem_check(tmp_path: Path):
    """A filename not in the allowlist must 404 before any disk lookup."""
    client, store, _ = _setup(tmp_path)
    session = store.mint("aa:bb:cc:dd:ee:ff")
    response = client.get(f"/boot/passwd?t={session.token}")
    assert response.status_code == 404


def test_path_traversal_attempts_are_rejected(tmp_path: Path):
    client, store, _ = _setup(tmp_path)
    session = store.mint("aa:bb:cc:dd:ee:ff")
    # FastAPI route matching alone blocks slashes, but the allowlist is the
    # belt-and-braces guard.
    response = client.get(f"/boot/..%2Fetc%2Fpasswd?t={session.token}")
    assert response.status_code in {404, 400}


def test_unknown_token_returns_401_uniformly(tmp_path: Path):
    client, _store, boot_dir = _setup(tmp_path)
    (boot_dir / "vmlinuz").write_bytes(b"payload")
    response = client.get("/boot/vmlinuz?t=not-a-real-token")
    assert response.status_code == 401
    assert response.json() == {"detail": "unauthorised"}


def test_missing_token_returns_422(tmp_path: Path):
    """FastAPI's Query(...) makes the token mandatory; missing → 422."""
    client, _store, boot_dir = _setup(tmp_path)
    (boot_dir / "vmlinuz").write_bytes(b"payload")
    response = client.get("/boot/vmlinuz")
    assert response.status_code == 422


def test_known_token_but_file_missing_returns_404(tmp_path: Path):
    """An allowlisted name that hasn't been built yet must 404, not 500."""
    client, store, _ = _setup(tmp_path)
    session = store.mint("aa:bb:cc:dd:ee:ff")
    response = client.get(f"/boot/vmlinuz?t={session.token}")
    assert response.status_code == 404


def test_boot_serving_disabled_when_no_boot_dir():
    app = create_app(sessions=BootSessionStore(), boot_dir=None)
    client = TestClient(app)
    response = client.get("/boot/vmlinuz?t=anything")
    assert response.status_code == 503
