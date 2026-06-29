"""Recipe structural tests for the dev/bootstrap fleetboot account.

We bake a known `fleetboot / fleetboot` account into the base image so
the greeter is actually usable on a fresh fleet before FreeIPA is
wired up. The test asserts the recipe creates that account (and only
that account — anything else should go through FreeIPA).
"""

from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
RECIPE = REPO_ROOT / "image" / "fleetboot-base.yaml"


def test_recipe_creates_fleetboot_bootstrap_account():
    text = RECIPE.read_text()
    # Username `fleetboot` is created with a home dir and bash shell.
    assert "useradd" in text
    assert "fleetboot" in text
    # Password is set non-interactively via chpasswd. Known weak
    # credential — the comment in the recipe warns admins to rotate.
    assert "chpasswd" in text
    assert "fleetboot:fleetboot" in text


def test_bootstrap_account_is_idempotent():
    """Re-running the recipe step on an image where the user already
    exists must be a no-op — otherwise an admin re-running a build
    fragment would get useradd errors."""
    text = RECIPE.read_text()
    # `id -u fleetboot` gate around the creation.
    assert "id -u fleetboot" in text


def test_bootstrap_account_home_is_outside_slash_home():
    """autofs takes over /home for FreeIPA users (auto.home wildcard
    match), which shadows any local /home/<name> directory at login
    time. The bootstrap account's home MUST be outside /home so its
    .Xauthority etc. can be created by lightdm."""
    text = RECIPE.read_text()
    assert "--home-dir /var/local/fleetboot" in text


def test_bootstrap_account_has_hardware_groups():
    """The account needs to be in audio/video/plugdev so the desktop
    is actually usable (sound, USB, screen settings) — otherwise
    'log in works but everything is broken' confusion."""
    text = RECIPE.read_text()
    for group in ("sudo", "audio", "video", "plugdev"):
        assert group in text
