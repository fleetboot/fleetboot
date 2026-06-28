"""Tests for the operational dashboard."""

from __future__ import annotations

import base64
import shutil
import stat
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from fleetboot.server.app import create_app
from fleetboot.server.boot_sessions import BootSessionStore
from fleetboot.server.registry import MachineRegistry


ADMIN = "the-admin-secret"


@pytest.fixture
def dashboard_root(tmp_path: Path) -> Path:
    """Build a minimal fleetboot repo layout the dashboard can read."""
    root = tmp_path / "repo"
    root.mkdir()
    # Makefile presence is the BuildJobManager's sanity check.
    # PHONY because there's a sibling `image/` directory; make would
    # otherwise think the `image` target is already up to date.
    (root / "Makefile").write_text(".PHONY: image\nimage:\n\techo built\n")
    profiles = root / "image" / "profiles"
    profiles.mkdir(parents=True)
    (profiles / "default").mkdir()
    (profiles / "default" / "extra-packages.list").write_text("")
    (profiles / "default" / "README.md").write_text(
        "# default profile\nThe baseline image.\n"
    )
    (profiles / "school").mkdir()
    (profiles / "school" / "extra-packages.list").write_text("extrepo\n")
    (profiles / "school" / "README.md").write_text(
        "# school profile\nWith LibreWolf.\n"
    )
    setup = profiles / "school" / "setup-chroot"
    setup.write_text("#!/bin/sh\nextrepo enable librewolf\n")
    setup.chmod(0o755)
    return root


def _auth_header(secret: str = ADMIN) -> dict[str, str]:
    return {"Authorization": "Basic " + base64.b64encode(
        f"admin:{secret}".encode()
    ).decode()}


def _client(dashboard_root: Path) -> TestClient:
    registry = MachineRegistry(dashboard_root / "machines.sqlite")
    app = create_app(
        sessions=BootSessionStore(),
        registry=registry,
        admin_secret=ADMIN,
        dashboard_repo_root=dashboard_root,
    )
    return TestClient(app)


# ---- Auth ---------------------------------------------------------------


def test_dashboard_requires_admin_credentials(dashboard_root: Path):
    client = _client(dashboard_root)
    response = client.get("/dashboard")
    assert response.status_code == 401
    assert "WWW-Authenticate" in response.headers


def test_dashboard_rejects_wrong_password(dashboard_root: Path):
    client = _client(dashboard_root)
    response = client.get("/dashboard", headers=_auth_header("wrong"))
    assert response.status_code == 401


def test_dashboard_accepts_correct_password(dashboard_root: Path):
    client = _client(dashboard_root)
    response = client.get("/dashboard", headers=_auth_header())
    assert response.status_code == 200
    assert "<h1>Machines</h1>" in response.text


def test_root_path_redirects_to_dashboard(dashboard_root: Path):
    client = _client(dashboard_root)
    response = client.get("/", headers=_auth_header(), follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"].endswith("/dashboard")


def test_dashboard_disabled_when_admin_secret_unset(dashboard_root: Path):
    """No admin secret → dashboard not mounted at all."""
    registry = MachineRegistry(dashboard_root / "m.sqlite")
    app = create_app(
        sessions=BootSessionStore(),
        registry=registry,
        admin_secret=None,
        dashboard_repo_root=dashboard_root,
    )
    client = TestClient(app)
    response = client.get("/dashboard", headers=_auth_header())
    assert response.status_code == 404


# ---- Machines ------------------------------------------------------------


def test_machines_page_lists_existing_machines(dashboard_root: Path):
    client = _client(dashboard_root)
    client.post(
        "/dashboard/machines",
        data={
            "mac": "aa:bb:cc:dd:ee:ff",
            "profile_name": "school",
            "architecture": "x86_64",
            "platform": "efi",
        },
        headers=_auth_header(),
    )
    response = client.get("/dashboard", headers=_auth_header())
    assert response.status_code == 200
    assert "aa:bb:cc:dd:ee:ff" in response.text
    assert "school" in response.text


def test_enrol_form_normalises_mac_and_redirects(dashboard_root: Path):
    client = _client(dashboard_root)
    response = client.post(
        "/dashboard/machines",
        data={
            "mac": "AA-BB-CC-DD-EE-FF",
            "profile_name": "default",
            "architecture": "x86_64",
            "platform": "efi",
            "serial_console": "1",
        },
        headers=_auth_header(),
        follow_redirects=False,
    )
    assert response.status_code == 303
    listing = client.get("/dashboard", headers=_auth_header())
    assert "aa:bb:cc:dd:ee:ff" in listing.text


def test_delete_machine_via_dashboard(dashboard_root: Path):
    client = _client(dashboard_root)
    client.post(
        "/dashboard/machines",
        data={
            "mac": "aa:bb:cc:dd:ee:ff",
            "profile_name": "default",
            "architecture": "x86_64",
            "platform": "efi",
        },
        headers=_auth_header(),
    )
    response = client.post(
        "/dashboard/machines/aa:bb:cc:dd:ee:ff/delete",
        headers=_auth_header(),
        follow_redirects=False,
    )
    assert response.status_code == 303
    listing = client.get("/dashboard", headers=_auth_header())
    # The MAC appears in the empty-form placeholder; tighten the assertion
    # to the table cell that would render the row.
    assert "<code>aa:bb:cc:dd:ee:ff</code>" not in listing.text


# ---- Profiles ------------------------------------------------------------


def test_profiles_page_lists_profile_dirs(dashboard_root: Path):
    client = _client(dashboard_root)
    response = client.get("/dashboard/profiles", headers=_auth_header())
    assert response.status_code == 200
    assert "default" in response.text
    assert "school" in response.text


def test_view_profile_renders_files(dashboard_root: Path):
    client = _client(dashboard_root)
    response = client.get(
        "/dashboard/profiles/school", headers=_auth_header()
    )
    assert response.status_code == 200
    assert "extrepo" in response.text
    assert "LibreWolf" in response.text


def test_view_unknown_profile_is_404(dashboard_root: Path):
    client = _client(dashboard_root)
    response = client.get(
        "/dashboard/profiles/nope", headers=_auth_header()
    )
    assert response.status_code == 404


def test_view_profile_with_invalid_name_rejected(dashboard_root: Path):
    """Names containing non-alnum/hyphen characters must be refused."""
    client = _client(dashboard_root)
    # A dot in the segment survives URL normalisation and reaches the
    # route, which our validator rejects.
    response = client.get(
        "/dashboard/profiles/foo.bar", headers=_auth_header()
    )
    assert response.status_code == 400


def test_save_profile_writes_files(dashboard_root: Path):
    client = _client(dashboard_root)
    response = client.post(
        "/dashboard/profiles/school",
        data={
            "extra_packages": "extrepo\nlibreoffice\n",
            "setup_chroot": "#!/bin/sh\necho yo\n",
            "readme": "# updated\n",
        },
        headers=_auth_header(),
        follow_redirects=False,
    )
    assert response.status_code == 303
    profile_dir = dashboard_root / "image" / "profiles" / "school"
    assert "libreoffice" in (profile_dir / "extra-packages.list").read_text()
    assert "updated" in (profile_dir / "README.md").read_text()
    assert "yo" in (profile_dir / "setup-chroot").read_text()
    # setup-chroot must come back executable.
    mode = (profile_dir / "setup-chroot").stat().st_mode
    assert mode & stat.S_IXUSR


def test_save_profile_removes_empty_setup_chroot(dashboard_root: Path):
    """Submitting an empty setup-chroot deletes any prior script."""
    client = _client(dashboard_root)
    profile_dir = dashboard_root / "image" / "profiles" / "school"
    assert (profile_dir / "setup-chroot").is_file()
    response = client.post(
        "/dashboard/profiles/school",
        data={"extra_packages": "", "setup_chroot": "  \n", "readme": ""},
        headers=_auth_header(),
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert not (profile_dir / "setup-chroot").exists()


# ---- Builds -------------------------------------------------------------


def test_builds_page_renders_with_no_jobs(dashboard_root: Path):
    client = _client(dashboard_root)
    response = client.get("/dashboard/builds", headers=_auth_header())
    assert response.status_code == 200
    assert "No builds yet" in response.text


def test_trigger_build_unknown_profile_is_404(dashboard_root: Path):
    client = _client(dashboard_root)
    response = client.post(
        "/dashboard/builds",
        data={"profile": "nope", "architecture": "amd64"},
        headers=_auth_header(),
        follow_redirects=False,
    )
    # An invalid profile name (containing dots/slashes) returns 400; an
    # unknown valid name returns 404.
    assert response.status_code == 404


def test_trigger_build_runs_make_and_records_job(dashboard_root: Path):
    """Use the trivial Makefile we set up — `make image` just echoes."""
    client = _client(dashboard_root)
    response = client.post(
        "/dashboard/builds",
        data={"profile": "default", "architecture": "amd64"},
        headers=_auth_header(),
        follow_redirects=False,
    )
    assert response.status_code == 303
    job_url = response.headers["location"]
    # The build runs in a background thread; poll for completion.
    import time

    for _ in range(50):
        detail = client.get(job_url, headers=_auth_header())
        if "succeeded" in detail.text or "failed" in detail.text:
            break
        time.sleep(0.1)
    detail = client.get(job_url, headers=_auth_header())
    assert detail.status_code == 200
    assert "succeeded" in detail.text
    # The job's stdout (literally "built\n" from our trivial Makefile)
    # should show up in the tail.
    assert "built" in detail.text


