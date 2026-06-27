"""Tests for /boot/{token}/{filename} — serving kernel/initrd/squashfs to
GRUB and live-boot under per-boot session token auth.

The token is in a PATH SEGMENT, not the query string: live-boot determines
the archive type from the URL's text after the last `.`, so a `?t=…` query
would mangle `.squashfs` into `squashfs?t=…` and the file would be reported
as "Unrecognised archive extension".

Validation is identical to /status: unknown token → 401, no enumeration
oracle for which-files-exist.
"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from fleetboot.server.app import ALLOWED_BOOT_FILES, create_app
from fleetboot.server.boot_sessions import BootSessionStore


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
    response = client.get(f"/boot/{session.token}/vmlinuz")
    assert response.status_code == 200
    assert response.content == b"\x7fELF...kernel-payload"


@pytest.mark.parametrize("filename", sorted(ALLOWED_BOOT_FILES))
def test_every_allowlisted_filename_resolves_when_present(
    tmp_path: Path, filename: str
):
    client, store, boot_dir = _setup(tmp_path)
    (boot_dir / filename).write_bytes(b"contents")
    session = store.mint("aa:bb:cc:dd:ee:ff")
    response = client.get(f"/boot/{session.token}/{filename}")
    assert response.status_code == 200
    assert response.content == b"contents"


def test_url_ends_with_real_file_extension(tmp_path: Path):
    """live-boot parses the archive type by sed-extracting after the last `.`.
    The URL we generate must end in the actual extension."""
    client, store, boot_dir = _setup(tmp_path)
    (boot_dir / "fleetboot-amd64.squashfs").write_bytes(b"sqsh")
    session = store.mint("aa:bb:cc:dd:ee:ff")
    url = f"/boot/{session.token}/fleetboot-amd64.squashfs"
    assert url.rsplit(".", 1)[1] == "squashfs"
    response = client.get(url)
    assert response.status_code == 200


def test_unknown_filename_returns_404_without_filesystem_check(tmp_path: Path):
    """A filename not in the allowlist must 404 before any disk lookup."""
    client, store, _ = _setup(tmp_path)
    session = store.mint("aa:bb:cc:dd:ee:ff")
    response = client.get(f"/boot/{session.token}/passwd")
    assert response.status_code == 404


def test_unknown_token_returns_401_uniformly(tmp_path: Path):
    client, _store, boot_dir = _setup(tmp_path)
    (boot_dir / "vmlinuz").write_bytes(b"payload")
    response = client.get("/boot/not-a-real-token/vmlinuz")
    assert response.status_code == 401
    assert response.json() == {"detail": "unauthorised"}


def test_known_token_but_file_missing_returns_404(tmp_path: Path):
    """An allowlisted name that hasn't been built yet must 404, not 500."""
    client, store, _ = _setup(tmp_path)
    session = store.mint("aa:bb:cc:dd:ee:ff")
    response = client.get(f"/boot/{session.token}/vmlinuz")
    assert response.status_code == 404


def test_boot_serving_disabled_when_no_boot_dir():
    app = create_app(sessions=BootSessionStore(), boot_dir=None)
    client = TestClient(app)
    response = client.get("/boot/anything/vmlinuz")
    assert response.status_code == 503
