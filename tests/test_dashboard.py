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
    assert "<h1>Machines" in response.text


def test_dashboard_emits_meta_refresh_when_requested(dashboard_root: Path):
    """`?refresh=5` switches the page into live-view mode."""
    client = _client(dashboard_root)
    response = client.get(
        "/dashboard?refresh=5", headers=_auth_header()
    )
    assert response.status_code == 200
    assert '<meta http-equiv="refresh" content="5">' in response.text


def test_dashboard_refresh_value_is_clamped(dashboard_root: Path):
    """Bogus values get clamped; this protects the server from a junk
    query param triggering 1-second hammering."""
    client = _client(dashboard_root)
    response = client.get(
        "/dashboard?refresh=999999", headers=_auth_header()
    )
    assert '<meta http-equiv="refresh" content="60">' in response.text


def test_dashboard_no_refresh_meta_when_unset(dashboard_root: Path):
    client = _client(dashboard_root)
    response = client.get("/dashboard", headers=_auth_header())
    assert "http-equiv=\"refresh\"" not in response.text


def test_events_page_supports_refresh(dashboard_root: Path):
    client = _client(dashboard_root)
    response = client.get(
        "/dashboard/events?refresh=5", headers=_auth_header()
    )
    assert response.status_code == 200
    assert '<meta http-equiv="refresh" content="5">' in response.text


def test_machines_page_colours_version_current_and_stale(
    dashboard_root: Path,
):
    """`build/<artefact>.version` sidecar is the source of truth for 'latest'.

    A machine whose reported boot_version matches the sidecar shows
    `version-current` (green). Anything else shows `version-stale` (orange).
    """
    build_dir = dashboard_root / "build"
    build_dir.mkdir()
    (build_dir / "fleetboot-school-amd64.version").write_text(
        "2026-06-28T22:00:00Z\n"
    )
    registry = MachineRegistry(dashboard_root / "machines.sqlite")
    app = create_app(
        sessions=BootSessionStore(),
        registry=registry,
        admin_secret=ADMIN,
        boot_dir=build_dir,
        dashboard_repo_root=dashboard_root,
    )
    client = TestClient(app)
    registry.enroll(
        mac="aa:bb:cc:dd:ee:01", profile_name="school",
        architecture="amd64", platform="efi",
    )
    registry.enroll(
        mac="aa:bb:cc:dd:ee:02", profile_name="school",
        architecture="amd64", platform="efi",
    )
    registry.update_boot_version(
        "aa:bb:cc:dd:ee:01", "2026-06-28T22:00:00Z",
    )
    registry.update_boot_version(
        "aa:bb:cc:dd:ee:02", "2026-06-27T10:00:00Z",
    )

    page = client.get("/dashboard", headers=_auth_header()).text
    assert "version-current" in page
    assert "version-stale" in page


def test_machine_detail_page_shows_all_fields(dashboard_root: Path):
    """The detail page is the place an admin goes to see EVERYTHING
    about one machine — registry fields, recent events, scratch mode,
    enrolled_by, etc."""
    client = _client(dashboard_root)
    client.post(
        "/dashboard/machines",
        data={
            "mac": "aa:bb:cc:dd:ee:ff",
            "profile_name": "school",
            "architecture": "x86_64",
            "platform": "efi",
            "scratch_mode": "persistent",
        },
        headers=_auth_header(),
    )
    response = client.get(
        "/dashboard/machines/aa:bb:cc:dd:ee:ff", headers=_auth_header()
    )
    assert response.status_code == 200
    body = response.text
    # Each field worth surfacing must appear.
    assert "aa:bb:cc:dd:ee:ff" in body
    assert "school" in body
    assert "persistent" in body  # scratch_mode
    assert "Recent boot events" in body


def test_set_reboot_command_and_then_clear(dashboard_root: Path):
    """The detail page's form persists / clears the reboot_command."""
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
    cmd = "curl http://pdu/reboot?port=6"
    client.post(
        "/dashboard/machines/aa:bb:cc:dd:ee:ff/reboot-command",
        data={"reboot_command": cmd},
        headers=_auth_header(),
    )
    body = client.get(
        "/dashboard/machines/aa:bb:cc:dd:ee:ff",
        headers=_auth_header(),
    ).text
    assert cmd in body
    # Empty submission clears it.
    client.post(
        "/dashboard/machines/aa:bb:cc:dd:ee:ff/reboot-command",
        data={"reboot_command": ""},
        headers=_auth_header(),
    )
    body = client.get(
        "/dashboard/machines/aa:bb:cc:dd:ee:ff",
        headers=_auth_header(),
    ).text
    # Form's input value is empty; the command title attr is gone too.
    assert "value=\"\"" in body or "value=''" in body


def test_delete_and_reboot_runs_stored_command_then_deletes(
    dashboard_root: Path, tmp_path: Path,
):
    """When the admin clicks delete+reboot, the stored command runs on
    the fleetboot host (subprocess.Popen with shell=True) and the
    machine row is then removed from the registry."""
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
    # Write a sentinel file; the "reboot command" just touches it.
    marker = tmp_path / "rebooted"
    client.post(
        "/dashboard/machines/aa:bb:cc:dd:ee:ff/reboot-command",
        data={"reboot_command": f"touch {marker}"},
        headers=_auth_header(),
    )
    response = client.post(
        "/dashboard/machines/aa:bb:cc:dd:ee:ff/delete-and-reboot",
        headers=_auth_header(),
        follow_redirects=False,
    )
    assert response.status_code == 303
    # subprocess.Popen is async; give it a moment.
    import time
    for _ in range(30):
        if marker.exists():
            break
        time.sleep(0.1)
    assert marker.exists(), "reboot command didn't run"
    # And the registry row is gone.
    listing = client.get("/dashboard", headers=_auth_header()).text
    assert "<code>aa:bb:cc:dd:ee:ff</code>" not in listing


def test_delete_and_reboot_falls_back_to_pdudaemon_when_no_explicit_command(
    dashboard_root: Path, tmp_path: Path,
):
    """If the machine has no explicit reboot_command but pdudaemon_host
    is set fleet-wide AND the machine has a hostname, delete+reboot
    should fire a curl to pdudaemon using the hostname as alias."""
    import subprocess
    import unittest.mock as mock

    client = _client(dashboard_root)
    # Register, give it a hostname, set fleet-wide pdudaemon host.
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
    # Hostname comes from a /status post normally — go straight to
    # registry for the test fixture.
    from fleetboot.server.registry import MachineRegistry
    reg = MachineRegistry(dashboard_root / "machines.sqlite")
    reg.update_hostname("aa:bb:cc:dd:ee:ff", "lab-pc-01")
    client.post(
        "/dashboard/settings",
        data={"pdudaemon_host": "prowl:16421"},
        headers=_auth_header(),
    )
    with mock.patch.object(subprocess, "Popen") as popen:
        response = client.post(
            "/dashboard/machines/aa:bb:cc:dd:ee:ff/delete-and-reboot",
            headers=_auth_header(),
            follow_redirects=False,
        )
    assert response.status_code == 303
    popen.assert_called_once()
    command = popen.call_args.args[0]
    assert "http://prowl:16421/power/control/reboot?alias=lab-pc-01" in command


def test_explicit_reboot_command_wins_over_pdudaemon(
    dashboard_root: Path, tmp_path: Path,
):
    """An explicit per-machine reboot_command must take precedence over
    the pdudaemon fallback — admins use explicit commands for the
    machines whose power isn't routed through pdudaemon."""
    import subprocess
    import unittest.mock as mock

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
    from fleetboot.server.registry import MachineRegistry
    reg = MachineRegistry(dashboard_root / "machines.sqlite")
    reg.update_hostname("aa:bb:cc:dd:ee:ff", "lab-pc-01")
    client.post(
        "/dashboard/settings",
        data={"pdudaemon_host": "prowl:16421"},
        headers=_auth_header(),
    )
    client.post(
        "/dashboard/machines/aa:bb:cc:dd:ee:ff/reboot-command",
        data={"reboot_command": "echo explicit"},
        headers=_auth_header(),
    )
    with mock.patch.object(subprocess, "Popen") as popen:
        client.post(
            "/dashboard/machines/aa:bb:cc:dd:ee:ff/delete-and-reboot",
            headers=_auth_header(),
            follow_redirects=False,
        )
    command = popen.call_args.args[0]
    assert command == "echo explicit"


def test_machine_detail_404_for_unknown_mac(dashboard_root: Path):
    client = _client(dashboard_root)
    response = client.get(
        "/dashboard/machines/aa:bb:cc:dd:ee:ff", headers=_auth_header()
    )
    assert response.status_code == 404


def test_machine_detail_supports_refresh_query_param(dashboard_root: Path):
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
    response = client.get(
        "/dashboard/machines/aa:bb:cc:dd:ee:ff?refresh=5",
        headers=_auth_header(),
    )
    assert response.status_code == 200
    assert '<meta http-equiv="refresh" content="5">' in response.text


def test_machines_page_shows_last_seen_and_stale_indicator(
    dashboard_root: Path,
):
    """Heartbeat-style timestamps surface as 'N min ago'; older than the
    stale threshold gets the version-stale class."""
    from datetime import datetime, timedelta, timezone

    from fleetboot.boot_states import BootState
    from fleetboot.server.boot_sessions import BootSessionStore

    sessions_db = dashboard_root / "sessions.sqlite"
    sessions = BootSessionStore(sessions_db)
    registry = MachineRegistry(dashboard_root / "machines.sqlite")
    registry.enroll(
        mac="aa:bb:cc:dd:ee:01", profile_name="default",
        architecture="x86_64", platform="efi",
    )
    registry.enroll(
        mac="aa:bb:cc:dd:ee:02", profile_name="default",
        architecture="x86_64", platform="efi",
    )
    # Mint + record state for both, so latest_state_at is "now-ish".
    a = sessions.mint("aa:bb:cc:dd:ee:01")
    b = sessions.mint("aa:bb:cc:dd:ee:02")
    sessions.record_state(a.token, BootState.NETWORK_UP)
    sessions.record_state(b.token, BootState.LOGIN_READY)
    # Backdate one of them to simulate a machine that went silent. SQLite
    # stores `datetime('now')`, so we patch the row directly.
    import sqlite3
    long_ago = (datetime.now(timezone.utc) - timedelta(hours=2)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    with sqlite3.connect(sessions_db) as connection:
        connection.execute(
            "UPDATE boot_sessions SET latest_state_at = ? WHERE token = ?",
            (long_ago, b.token),
        )
    app = create_app(
        sessions=sessions,
        registry=registry,
        admin_secret=ADMIN,
        dashboard_repo_root=dashboard_root,
    )
    client = TestClient(app)
    page = client.get("/dashboard", headers=_auth_header()).text
    # Fresh row should show something like "0s ago"/"1s ago"; stale row
    # should use the version-stale class.
    assert "ago" in page
    assert "version-stale" in page


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


# ---- Events ------------------------------------------------------------


def test_events_page_renders_logged_history(dashboard_root: Path):
    """A boot event written by /status shows up on the events page."""
    client = _client(dashboard_root)
    # Mint a session and record a state. That triggers the side-effect
    # the dashboard reads from the registry.
    sessions = client.app.state.sessions
    sessions.mint("aa:bb:cc:dd:ee:ff")
    # Record via the API so the /status -> registry.log_boot_event hook
    # fires (not by writing directly to the store).
    session = list(sessions.active_sessions())[0]
    response = client.post(
        "/status",
        json={"state": "network_up"},
        headers={"Authorization": f"Bearer {session.token}"},
    )
    assert response.status_code == 200
    response = client.post(
        "/status",
        json={"state": "nfs_mounted"},
        headers={"Authorization": f"Bearer {session.token}"},
    )
    assert response.status_code == 200

    page = client.get("/dashboard/events", headers=_auth_header())
    assert page.status_code == 200
    assert "aa:bb:cc:dd:ee:ff" in page.text
    assert "network_up" in page.text
    assert "nfs_mounted" in page.text


def test_events_page_per_mac_filter(dashboard_root: Path):
    client = _client(dashboard_root)
    sessions = client.app.state.sessions
    s1 = sessions.mint("aa:bb:cc:dd:ee:01")
    s2 = sessions.mint("aa:bb:cc:dd:ee:02")
    client.post(
        "/status",
        json={"state": "network_up"},
        headers={"Authorization": f"Bearer {s1.token}"},
    )
    client.post(
        "/status",
        json={"state": "network_up"},
        headers={"Authorization": f"Bearer {s2.token}"},
    )

    response = client.get(
        "/dashboard/events?mac=aa:bb:cc:dd:ee:01", headers=_auth_header()
    )
    assert response.status_code == 200
    assert "aa:bb:cc:dd:ee:01" in response.text
    # The ?mac filter should hide events from the other MAC.
    assert "aa:bb:cc:dd:ee:02" not in response.text


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


def test_concurrent_build_flashes_friendly_error(dashboard_root: Path):
    """A second build submitted while the first is running redirects back
    to the builds list with an `error=already-running` flash, not a 500."""
    # Use a Makefile that takes long enough that the second submission
    # arrives while the first is still in flight.
    (dashboard_root / "Makefile").write_text(
        ".PHONY: image\nimage:\n\tsleep 0.3\n"
    )
    client = _client(dashboard_root)
    first = client.post(
        "/dashboard/builds",
        data={"profile": "default", "architecture": "amd64"},
        headers=_auth_header(),
        follow_redirects=False,
    )
    assert first.status_code == 303
    second = client.post(
        "/dashboard/builds",
        data={"profile": "default", "architecture": "amd64"},
        headers=_auth_header(),
        follow_redirects=False,
    )
    assert second.status_code == 303
    assert "error=already-running" in second.headers["location"]
    # The page itself shows the flash.
    page = client.get(
        "/dashboard/builds?error=already-running", headers=_auth_header()
    )
    assert "already running" in page.text


def test_build_detail_follow_link_and_autoscroll(dashboard_root: Path):
    """`?follow=1` renders the autoscroll script + a 'stop following' link."""
    client = _client(dashboard_root)
    first = client.post(
        "/dashboard/builds",
        data={"profile": "default", "architecture": "amd64"},
        headers=_auth_header(),
        follow_redirects=False,
    )
    job_url = first.headers["location"]
    plain = client.get(job_url, headers=_auth_header())
    assert plain.status_code == 200
    assert ">follow log<" in plain.text
    assert "scrollHeight" not in plain.text

    follow = client.get(job_url + "?follow=1", headers=_auth_header())
    assert follow.status_code == 200
    assert ">stop following<" in follow.text
    assert "scrollHeight" in follow.text


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


