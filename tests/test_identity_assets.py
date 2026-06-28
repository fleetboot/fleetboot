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


# ---- Keytab fetch ----------------------------------------------------------


def test_fetch_keytab_script_is_executable():
    script = IDENTITY_DIR / "fetch-keytab"
    assert script.is_file()
    mode = script.stat().st_mode
    assert mode & stat.S_IXUSR


def test_fetch_keytab_script_uses_cmdline_token():
    script = (IDENTITY_DIR / "fetch-keytab").read_text()
    # Reuses the reporter's cmdline parser — no duplicate cmdline parsing.
    assert "fleetboot.reporter.cmdline" in script
    assert "/enrol/" in script
    assert "/keytab" in script


def test_fetch_keytab_writes_keytab_with_safe_perms():
    script = (IDENTITY_DIR / "fetch-keytab").read_text()
    # The keytab is sensitive — must be mode 0600 once written.
    assert "0o600" in script or "chmod(0o600)" in script
    assert "/etc/fleetboot/enrol.keytab" in script


def test_keytab_fetch_unit_is_oneshot_and_ordered():
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    parser.read(IDENTITY_DIR / "fleetboot-keytab-fetch.service")
    assert parser["Service"]["Type"] == "oneshot"
    assert "network-online.target" in parser["Unit"]["After"]
    # Critically: runs BEFORE the enrolment unit so the keytab is there.
    assert "fleetboot-freeipa-enroll.service" in parser["Unit"]["Before"]


def test_keytab_fetch_unit_skips_when_already_enrolled():
    text = (IDENTITY_DIR / "fleetboot-keytab-fetch.service").read_text()
    assert "ConditionPathExists=!/etc/ipa/default.conf" in text


def test_keytab_fetch_unit_skips_when_no_cmdline_token():
    text = (IDENTITY_DIR / "fleetboot-keytab-fetch.service").read_text()
    assert "ConditionKernelCommandLine=fleetboot.boot_token" in text


# ---- Shared NFS mount -----------------------------------------------------


def test_auto_shared_map_exists_and_uses_krb5p():
    text = (IDENTITY_DIR / "auto.shared").read_text()
    assert "nfs4" in text
    assert "sec=krb5p" in text
    assert "<NFS_SERVER>" in text
    # The wildcard substitution `&` is what makes /shared/<X> resolve to
    # /export/shared/<X> on the server.
    assert "&" in text
    assert "/export/shared/" in text


def test_enroll_freeipa_substitutes_nfs_server():
    script = (IDENTITY_DIR / "enroll-freeipa").read_text()
    # The substitution step is what makes the autofs maps usable; without
    # it /home/<user> and /shared/<X> point at "<NFS_SERVER>" (no DNS).
    assert "IPA_NFS_SERVER" in script
    assert "/etc/auto.home" in script
    assert "/etc/auto.shared" in script
    # The sed line that does the substitution.
    assert "<NFS_SERVER>" in script and "sed" in script


# ---- NFS server-side templates --------------------------------------------


NFS_TEMPLATES = REPO_ROOT / "nfs"


def test_exports_template_exists_and_uses_krb5p():
    text = (NFS_TEMPLATES / "exports.template").read_text()
    # The export config must NOT degrade to sec=sys; that would expose
    # /home to unauth'd hosts.
    assert "sec=krb5p" in text
    assert "sec=sys" not in text
    # NFSv4 pseudo-root.
    assert "fsid=0" in text
    # Both branches present.
    assert "/export/home" in text
    assert "/export/shared" in text


def test_idmapd_template_has_domain_placeholder():
    text = (NFS_TEMPLATES / "idmapd.conf.template").read_text()
    assert "__IPA_DOMAIN__" in text
    assert "[General]" in text


def test_setup_nfs_server_is_executable():
    script = REPO_ROOT / "scripts" / "setup-nfs-server.sh"
    assert script.is_file()
    mode = script.stat().st_mode
    assert mode & stat.S_IXUSR


def test_setup_nfs_server_uses_ipa_getkeytab():
    text = (REPO_ROOT / "scripts" / "setup-nfs-server.sh").read_text()
    # The script must get a Kerberos service keytab — not rely on
    # password-based auth or unauthenticated NFS.
    assert "ipa-getkeytab" in text
    assert "/etc/krb5.keytab" in text
    # And it must lay the templates down where the daemons expect them.
    assert "/etc/exports" in text
    assert "/etc/idmapd.conf" in text
