"""Tests for the docker-compose deployment.

Deployment is the one part of the project that isn't exercised by any
other test: a broken compose file fails on someone's new machine, at
install time, with the fleet down. These tests pin the invariants a
deployment depends on — above all that **tftpjail ships with the
control plane**, since fleetboot without it serves boot assets that
nothing can ask for.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"
FULL_STACK_FILE = REPO_ROOT / "deploy" / "full-stack" / "compose.yml"
DEPLOY_README = REPO_ROOT / "deploy" / "README.md"


@pytest.fixture(scope="module")
def compose() -> dict:
    return yaml.safe_load(COMPOSE_FILE.read_text())


@pytest.fixture(scope="module")
def full_stack() -> dict:
    return yaml.safe_load(FULL_STACK_FILE.read_text())


# ---- The control plane ships with its boot server -----------------------


def test_compose_defines_both_fleetboot_and_tftpjail(compose: dict):
    """The pair is the deployable unit. Shipping one without the other
    produces a fleet that cannot boot."""
    assert set(compose["services"]) == {"fleetboot", "tftpjail"}


def test_tftpjail_builds_from_the_workspace_parent(compose: dict):
    """The image needs both trees: generic tftpjail plus fleetboot's
    tftp_glue. That's why the build context is the parent directory and
    why a deployment needs the two checkouts side by side."""
    build = compose["services"]["tftpjail"]["build"]
    assert build["context"] == ".."
    assert build["dockerfile"] == "fleetboot/docker/Dockerfile.tftpjail"

    dockerfile = REPO_ROOT / "docker" / "Dockerfile.tftpjail"
    body = dockerfile.read_text()
    assert "COPY tftpjail/tftpjail" in body
    assert "COPY fleetboot/fleetboot/tftp_glue" in body


def test_tftpjail_uses_host_networking_for_udp_69(compose: dict):
    """TFTP across a NAT boundary is famously miserable, and every
    client on the LAN has to reach UDP/69 directly."""
    assert compose["services"]["tftpjail"]["network_mode"] == "host"


def test_tftpjail_waits_for_the_control_plane(compose: dict):
    """tftpjail mints boot sessions over fleetboot's HTTP API, so
    starting it first just produces failed requests."""
    assert "fleetboot" in compose["services"]["tftpjail"]["depends_on"]


def test_both_services_restart_unless_stopped(compose: dict):
    """The stack has to come back after a host reboot without anyone
    logging in to start it."""
    for name, service in compose["services"].items():
        assert service.get("restart") == "unless-stopped", name


def test_the_two_services_share_the_boot_artefacts(compose: dict):
    """tftpjail serves kernel and initrd from the same build/ tree
    fleetboot serves the squashfs from — read-only on its side."""
    assert any(
        volume.endswith("/build:ro")
        for volume in compose["services"]["tftpjail"]["volumes"]
    )


def test_secrets_have_no_defaults(compose: dict):
    """Both secrets use compose's `:?` form, so a missing .env fails
    loudly at `up` instead of silently deploying a known-value secret."""
    environment = compose["services"]["fleetboot"]["environment"]
    for key in ("FLEETBOOT_ADMIN_SECRET", "FLEETBOOT_MINT_SECRET"):
        assert ":?" in environment[key], key
    assert ":?" in compose["services"]["tftpjail"]["environment"][
        "CLIENT_BASE_URL"
    ]


# ---- Healthcheck --------------------------------------------------------


def test_healthcheck_probes_the_unauthenticated_route(compose: dict):
    """`/` redirects to the dashboard, which 401s without Basic auth —
    probing it marks a perfectly healthy container unhealthy forever.
    /healthz is the only route that answers without credentials."""
    healthcheck = compose["services"]["fleetboot"]["healthcheck"]
    probe = " ".join(healthcheck["test"])
    assert "/healthz" in probe


def test_healthz_answers_without_credentials():
    from fastapi.testclient import TestClient

    from fleetboot.server.app import create_app
    from fleetboot.server.boot_sessions import BootSessionStore

    client = TestClient(create_app(sessions=BootSessionStore()))
    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_healthz_says_nothing_about_the_fleet():
    """A probe endpoint that needs no auth must leak nothing — no
    version, no configuration, no machine state."""
    from fastapi.testclient import TestClient

    from fleetboot.server.app import create_app
    from fleetboot.server.boot_sessions import BootSessionStore

    client = TestClient(create_app(sessions=BootSessionStore()))
    assert set(client.get("/healthz").json()) == {"ok"}


# ---- The full-stack example ---------------------------------------------


def test_full_stack_includes_both_projects(full_stack: dict):
    """The example composes fleetboot's compose file with fleetid's
    rather than redefining either — a copied service definition is a
    definition that goes stale."""
    assert "services" not in full_stack
    paths = [entry["path"] for entry in full_stack["include"]]
    assert "../../docker-compose.yml" in paths
    assert any("fleetid" in path for path in paths)


def test_full_stack_gives_each_project_its_own_env_file(full_stack: dict):
    """fleetboot's secrets and fleetid's realm settings live in
    different .env files; `include` resolves each against its own
    project directory."""
    for entry in full_stack["include"]:
        assert "env_file" in entry, entry["path"]
    env_files = [entry["env_file"] for entry in full_stack["include"]]
    assert "../../.env" in env_files
    assert any("fleetid" in path for path in env_files)


def test_full_stack_lets_fleetid_live_elsewhere(full_stack: dict):
    """A sibling checkout is the default, not a requirement."""
    rendered = FULL_STACK_FILE.read_text()
    assert "${FLEETID_DIR:-" in rendered


def test_fleetid_services_stay_profile_gated():
    """FreeIPA claims host 443/389/88/464 and the NFS container is
    privileged with host networking. Including them must not mean
    starting them: a bare `up` brings up the boot plane only."""
    fleetid_compose = (
        REPO_ROOT.parent / "fleetid" / "compose" / "docker-compose.yml"
    )
    if not fleetid_compose.is_file():
        pytest.skip("fleetid checkout not present alongside fleetboot")
    services = yaml.safe_load(fleetid_compose.read_text())["services"]
    assert services["ipa"]["profiles"] == ["ipa"]
    assert services["nfs"]["profiles"] == ["nfs"]


# ---- The guide ----------------------------------------------------------


def test_deploy_guide_covers_the_host_prerequisites():
    """Each of these has cost someone a broken deployment: the port 69
    squatter, the sibling checkout, and the baked-in client URL."""
    guide = DEPLOY_README.read_text()
    for needle in ("tftpd-hpa", "CLIENT_BASE_URL", "git clone",
                   "nfs-kernel-server", "healthz"):
        assert needle in guide, needle
