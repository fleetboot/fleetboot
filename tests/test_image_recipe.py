"""Fast structural tests for the debos image recipe.

These do not run a real image build (that's too slow for `make test`). They
parse the YAML, walk the action list, and assert that the wiring we depend on
in the rest of the system is still intact:

  - the recipe is valid YAML after debos's text-template directives are stripped,
  - it declares an architecture and uses the actions we expect,
  - it overlays our reporter package, units, and PAM hook at the right paths,
  - it consumes the admin customisation contract,
  - it produces a squashfs at the end.

The slow `make image-smoke` actually builds and boots the result.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]
RECIPE_PATH = REPO_ROOT / "image" / "fleetboot-base.yaml"
CUSTOM_DIR = REPO_ROOT / "image" / "custom"


# debos pre-processes recipes through Go's text/template. For structural tests
# we strip the directives so PyYAML can parse the result. We distinguish:
#   {{ $x := ... }}   assignment directives -- emit nothing in real expansion,
#                     so we delete them entirely (including their line's newline).
#   {{- ... -}}       whitespace-trimming directives -- treat like assignments
#                     for our purposes; delete with surrounding whitespace.
#   {{ X }}           value-producing directives -- substitute a placeholder.
_ASSIGNMENT_DIRECTIVE = re.compile(
    r"^[ \t]*\{\{[^}]*:=[^{]*\}\}[ \t]*\n?", re.MULTILINE
)
_TRIM_DIRECTIVE = re.compile(r"\s*\{\{-.*?-\}\}\s*", re.DOTALL)
_VALUE_DIRECTIVE = re.compile(r"\{\{.*?\}\}", re.DOTALL)


def _render_for_structure(text: str) -> str:
    """Strip template directives, keeping a parseable YAML skeleton."""
    text = _ASSIGNMENT_DIRECTIVE.sub("", text)
    text = _TRIM_DIRECTIVE.sub("\n", text)
    text = _VALUE_DIRECTIVE.sub("placeholder", text)
    return text


@pytest.fixture(scope="module")
def recipe() -> dict:
    text = RECIPE_PATH.read_text()
    return yaml.safe_load(_render_for_structure(text))


def _actions_of_type(recipe: dict, action_type: str) -> list[dict]:
    return [a for a in recipe["actions"] if a.get("action") == action_type]


def test_recipe_declares_architecture(recipe: dict):
    assert "architecture" in recipe
    # The value is a template placeholder in our stripped view; just confirm
    # the key exists at the top level.


def test_recipe_starts_with_bootstrap_action(recipe: dict):
    """The base rootfs must come from a bootstrap action; that's the foundation."""
    first = recipe["actions"][0]
    assert first["action"] in {"mmdebstrap", "debootstrap"}


def test_recipe_installs_core_packages(recipe: dict):
    """The packages our reporter and netboot path depend on must be present."""
    apt_actions = _actions_of_type(recipe, "apt")
    assert apt_actions, "expected at least one apt action"
    all_packages = {pkg for a in apt_actions for pkg in a.get("packages", [])}
    required = {
        "python3",          # runs the reporter
        "python3-httpx",    # reporter's HTTP client
        "systemd",          # boots the image
        "live-boot",        # turns the squashfs into a netbootable root
        "ifupdown",         # gets the network up so network_up is meaningful
    }
    missing = required - all_packages
    assert not missing, f"recipe is missing required packages: {sorted(missing)}"


def test_reporter_package_is_overlaid_at_dist_packages(recipe: dict):
    """The reporter Python package must land where the systemd units expect it."""
    overlays = _actions_of_type(recipe, "overlay")
    matches = [
        a
        for a in overlays
        if "fleetboot" in (a.get("source", "") or "")
        and "dist-packages/fleetboot" in (a.get("destination", "") or "")
    ]
    assert matches, "no overlay action installs the fleetboot python package"


def test_systemd_units_overlaid_into_system_dir(recipe: dict):
    overlays = _actions_of_type(recipe, "overlay")
    matches = [
        a
        for a in overlays
        if "systemd" in (a.get("source", "") or "")
        and "/etc/systemd/system" in (a.get("destination", "") or "")
    ]
    assert matches, "no overlay action installs the systemd units"


def test_no_pam_hook_overlaid(recipe: dict):
    """The PAM session hook was removed (it fired for lightdm's own
    session, not real human logins, which made the signal misleading).
    The recipe must not re-introduce the overlay."""
    overlays = _actions_of_type(recipe, "overlay")
    matches = [
        a
        for a in overlays
        if (a.get("source", "") or "").rstrip("/").endswith("/pam")
        or (a.get("source", "") or "").rstrip("/").endswith("./pam")
    ]
    assert not matches, "PAM hook overlay must not exist anymore"


# NB: The base recipe used to install xfce4 + lightdm directly and set
# graphical.target as the default. With the profile-inheritance refactor
# all of that moved out to image/profiles/xfce-desktop/ (and the other
# desktop example profiles). The new shape is checked by tests in
# tests/test_profile_examples.py: the base recipe MUST NOT install GUI
# packages, and MUST set multi-user.target as the default.


def test_recipe_consumes_profile_extras(recipe: dict):
    """The recipe must apply the profile's extra packages, overlay, and
    setup-chroot — that's the contract for built-in profiles."""
    runs = _actions_of_type(recipe, "run")
    commands = " \n".join((a.get("command", "") or "") for a in runs)
    # The profile is staged into a durable build-only path (not /tmp,
    # where apt-get install can wipe it).
    assert "fleetboot-build/profile/extra-packages.list" in commands
    # The overlay copy uses the resolver's merged tree so a parent
    # profile's overlay (e.g. logo's wallpaper) reaches the image.
    assert "profiles_resolved/" in commands
    assert "setup-chroot" in commands


def test_login_console_unit_always_enabled(recipe: dict):
    """Once we ship a display manager, fleetboot-login-console unconditionally
    enables — no need for the previous 'only if present' guard."""
    runs = _actions_of_type(recipe, "run")
    commands = " \n".join((a.get("command", "") or "") for a in runs)
    assert "systemctl enable fleetboot-login-console.service" in commands


def test_freeipa_client_packages_present(recipe: dict):
    """sssd / freeipa-client / nfs-common / krb5-user must be installed.

    Without these the image has no FreeIPA identity and no Kerberos NFS,
    which the lockdown model explicitly relies on.
    """
    apt_actions = _actions_of_type(recipe, "apt")
    all_packages = {pkg for a in apt_actions for pkg in a.get("packages", [])}
    required = {
        "freeipa-client",
        "sssd",
        "sssd-ipa",
        "libpam-sss",
        "libnss-sss",
        "krb5-user",
        "nfs-common",
        "autofs",
    }
    missing = required - all_packages
    assert not missing, f"recipe is missing identity packages: {sorted(missing)}"


def test_identity_assets_overlaid(recipe: dict):
    """The IPA enrolment helper, its systemd unit, and the autofs map ship."""
    overlays = _actions_of_type(recipe, "overlay")
    matches = [
        a
        for a in overlays
        if "identity" in (a.get("source", "") or "")
        and "identity" in (a.get("destination", "") or "")
    ]
    assert matches, "no overlay action installs the identity assets"


def test_freeipa_enroll_service_enabled(recipe: dict):
    """A run action must enable the FreeIPA enrolment oneshot."""
    runs = _actions_of_type(recipe, "run")
    found = any(
        "systemctl enable fleetboot-freeipa-enroll.service" in (a.get("command", "") or "")
        for a in runs
    )
    assert found, "fleetboot-freeipa-enroll.service is not enabled in the recipe"


def test_keytab_fetch_service_enabled_and_script_installed(recipe: dict):
    """The keytab-fetch oneshot must land and be enabled, and the fetch
    script must be installed under /usr/local/lib/fleetboot/."""
    runs = _actions_of_type(recipe, "run")
    commands = " \n".join((a.get("command", "") or "") for a in runs)
    assert "fetch-keytab" in commands
    assert "fleetboot-keytab-fetch.service" in commands
    assert "systemctl enable fleetboot-keytab-fetch.service" in commands


def test_autofs_wired_to_auto_home(recipe: dict):
    """The image must register our /home -> /etc/auto.home mapping."""
    runs = _actions_of_type(recipe, "run")
    commands = " \n".join((a.get("command", "") or "") for a in runs)
    assert "/etc/auto.home" in commands
    assert "auto.master" in commands


def test_autofs_wired_to_auto_shared(recipe: dict):
    """The image must register our /shared -> /etc/auto.shared mapping
    so group-scoped NFS mounts work."""
    runs = _actions_of_type(recipe, "run")
    commands = " \n".join((a.get("command", "") or "") for a in runs)
    assert "/etc/auto.shared" in commands


def test_recipe_does_not_install_a_pam_session_hook(recipe: dict):
    """We used to install a PAM hook that reported `user_logged_in` on
    any session open. That fired for lightdm's own session (not just
    real users), making the signal misleading. The hook was removed
    and the state was dropped — the recipe must NOT re-introduce it."""
    runs = _actions_of_type(recipe, "run")
    commands = " \n".join((a.get("command", "") or "") for a in runs)
    assert "fleetboot-session-hook" not in commands
    assert "/etc/pam.d/common-session" not in commands


def test_network_up_unit_is_enabled(recipe: dict):
    runs = _actions_of_type(recipe, "run")
    enabled = any(
        "systemctl enable fleetboot-network-up.service" in (a.get("command", "") or "")
        for a in runs
    )
    assert enabled, "fleetboot-network-up.service is not enabled in the recipe"


def test_recipe_consumes_admin_extra_packages(recipe: dict):
    """The recipe must read image/custom/extra-packages.list."""
    runs = _actions_of_type(recipe, "run")
    found = any(
        "extra-packages.list" in (a.get("command", "") or "") for a in runs
    )
    assert found, "recipe does not consume image/custom/extra-packages.list"


def test_recipe_consumes_admin_overlay(recipe: dict):
    runs = _actions_of_type(recipe, "run")
    found = any(
        "custom/overlay" in (a.get("command", "") or "") for a in runs
    )
    assert found, "recipe does not apply the admin overlay tree"


def test_recipe_runs_admin_hooks(recipe: dict):
    """Both pre-build and post-build hooks must be invoked."""
    commands = " \n".join(
        (a.get("command", "") or "") for a in _actions_of_type(recipe, "run")
    )
    assert "hooks/pre-build" in commands
    assert "hooks/post-build" in commands


def test_recipe_extracts_kernel_and_initrd(recipe: dict):
    """The smoke test needs vmlinuz and initrd.img alongside the squashfs."""
    commands = " \n".join(
        (a.get("command", "") or "") for a in _actions_of_type(recipe, "run")
    )
    assert "vmlinuz" in commands
    assert "initrd" in commands


def test_recipe_packs_a_squashfs(recipe: dict):
    """The final artifact must be a squashfs."""
    commands = " \n".join(
        (a.get("command", "") or "") for a in _actions_of_type(recipe, "run")
    )
    assert "mksquashfs" in commands


# ---------- customisation contract files ----------


def test_custom_dir_exists():
    assert CUSTOM_DIR.is_dir(), "image/custom/ contract directory missing"


def test_custom_readme_documents_the_contract():
    readme = (CUSTOM_DIR / "README.md").read_text()
    # The four contract points must all be documented.
    assert "extra-packages.list" in readme
    assert "overlay/" in readme
    assert "hooks/pre-build" in readme
    assert "hooks/post-build" in readme
    assert "local.yaml" in readme


def test_extra_packages_list_exists_and_is_safe_by_default():
    """The file must exist so the recipe overlay never fails. By default it
    must list zero real packages (only comments/blanks) so a fresh checkout
    builds the canonical example."""
    path = CUSTOM_DIR / "extra-packages.list"
    assert path.is_file()
    lines = [
        line.strip()
        for line in path.read_text().splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert lines == [], (
        "extra-packages.list must be empty by default (comments only); "
        "admins customise it in their own deployments"
    )


def test_overlay_and_hooks_dirs_exist():
    assert (CUSTOM_DIR / "overlay").is_dir()
    assert (CUSTOM_DIR / "hooks").is_dir()
