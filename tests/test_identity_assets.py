"""Structural tests for the identity assets shipped in image/identity/.

The actual FreeIPA enrolment + Kerberos NFSv4 mount is end-to-end against a
real IPA server and lives outside `make test`. Here we only assert that the
files exist, are correctly shaped, and would land where the recipe says.
"""

from __future__ import annotations

import configparser
import os
import stat
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
IDENTITY_DIR = REPO_ROOT / "image" / "identity"


def test_identity_dir_exists():
    assert IDENTITY_DIR.is_dir(), "image/identity/ missing"


def test_enroll_script_is_executable():
    script = IDENTITY_DIR / "enroll-freeipa"
    assert script.is_file()
    mode = script.stat().st_mode
    assert mode & stat.S_IXUSR, "enroll-freeipa must be executable"


def test_enroll_script_invokes_ipa_client_install():
    script = (IDENTITY_DIR / "enroll-freeipa").read_text()
    assert "ipa-client-install" in script
    # Unattended and using the keytab path the systemd unit requires.
    assert "--unattended" in script
    assert "--keytab=" in script


def test_enroll_script_skips_when_already_enrolled():
    """We must be idempotent — re-runs after the first boot must no-op."""
    script = (IDENTITY_DIR / "enroll-freeipa").read_text()
    assert "/etc/ipa/default.conf" in script


def test_systemd_enroll_unit_is_parseable_and_gated():
    """systemd allows multiple `ConditionPathExists=` lines, but
    configparser collapses duplicate keys — so we check the raw text."""
    text = (IDENTITY_DIR / "fleetboot-freeipa-enroll.service").read_text()

    parser = configparser.ConfigParser(strict=False, interpolation=None)
    parser.read_string(text)
    assert parser["Service"]["Type"] == "oneshot"
    assert "network-online.target" in parser["Unit"]["After"]

    # Both gates must be present on their own lines: only run if config has
    # been provisioned AND we are not enrolled yet.
    assert "ConditionPathExists=/etc/fleetboot/identity.conf" in text
    assert "ConditionPathExists=!/etc/ipa/default.conf" in text


def test_autofs_map_uses_kerberos_nfsv4():
    """The home map MUST use krb5p — anything weaker leaks home dirs on a
    rogue-laptop attack the design explicitly defends against."""
    contents = (IDENTITY_DIR / "auto.home").read_text()
    assert "nfs4" in contents
    assert "sec=krb5p" in contents
    # The map uses & to substitute the requested user.
    assert "&" in contents


def test_autofs_map_has_placeholder_for_nfs_server():
    """Admins / the enrolment step substitute the server FQDN in."""
    contents = (IDENTITY_DIR / "auto.home").read_text()
    assert "<NFS_SERVER>" in contents
