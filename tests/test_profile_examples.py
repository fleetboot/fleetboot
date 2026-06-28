"""Structural sanity checks for the example profiles in image/profiles/.

These tests are content-aware (they check the actual packages shipped)
because the profiles are the user-facing API of fleetboot. Changing
them silently is more dangerous than changing internal code.
"""

from __future__ import annotations

from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
PROFILES_DIR = REPO_ROOT / "image" / "profiles"

DESKTOP_PROFILES = (
    "xfce-desktop", "gnome-desktop", "kde-desktop", "cinnamon-desktop",
)
GRAPHICS_PROFILES = ("amd-graphics", "nvidia-graphics")


@pytest.mark.parametrize(
    "name",
    DESKTOP_PROFILES + GRAPHICS_PROFILES + ("default", "school"),
)
def test_profile_dir_and_readme_exist(name: str):
    p = PROFILES_DIR / name
    assert p.is_dir(), f"image/profiles/{name}/ missing"
    assert (p / "README.md").is_file(), f"{name} has no README"


@pytest.mark.parametrize("name", DESKTOP_PROFILES)
def test_desktop_profile_sets_graphical_target(name: str):
    """Every desktop profile must flip the systemd default to
    `graphical.target` — otherwise the base recipe's text-console
    default leaves the user without a login screen."""
    setup = PROFILES_DIR / name / "setup-chroot"
    assert setup.is_file(), f"{name} has no setup-chroot"
    text = setup.read_text()
    assert "systemctl set-default graphical.target" in text


@pytest.mark.parametrize("name", DESKTOP_PROFILES)
def test_desktop_profile_installs_x_server(name: str):
    """Every desktop needs an X server until we add a Wayland-only
    variant. Catch a profile that forgot it before debos finds out."""
    packages = (PROFILES_DIR / name / "extra-packages.list").read_text()
    assert "xserver-xorg" in packages


@pytest.mark.parametrize("name", DESKTOP_PROFILES)
def test_desktop_profile_installs_a_display_manager(name: str):
    """At least one of the common DMs must appear — without one, login
    never works."""
    packages = (PROFILES_DIR / name / "extra-packages.list").read_text()
    dms = ("lightdm", "gdm3", "sddm")
    assert any(dm in packages for dm in dms), (
        f"{name} has no display manager in extra-packages.list"
    )


def test_default_profile_does_not_install_a_desktop():
    """The whole point of restructuring was making `default` thin.
    Guard against regressions."""
    packages = (PROFILES_DIR / "default" / "extra-packages.list").read_text()
    forbidden = (
        "xfce4", "lightdm", "gdm3", "sddm",
        "gnome-shell", "kde-plasma-desktop",
        "cinnamon-desktop-environment",
        "xserver-xorg",
    )
    for token in forbidden:
        assert token not in packages, (
            f"`default` should be GUI-less but ships {token}"
        )


def test_school_profile_declares_xfce_desktop_parent():
    parent_file = PROFILES_DIR / "school" / "parent"
    assert parent_file.is_file(), "school/ must declare a parent now"
    assert "xfce-desktop" in parent_file.read_text()


def test_school_profile_still_installs_extrepo():
    """LibreWolf comes via extrepo; the change to inherit from
    xfce-desktop must not drop the school-specific contribution."""
    packages = (PROFILES_DIR / "school" / "extra-packages.list").read_text()
    assert "extrepo" in packages


@pytest.mark.parametrize("name", GRAPHICS_PROFILES)
def test_graphics_profile_is_a_mixin_no_parent(name: str):
    """Graphics profiles are designed to stack with a desktop, not to
    stand alone — they shouldn't declare a parent themselves."""
    assert not (PROFILES_DIR / name / "parent").exists()


def test_base_recipe_no_longer_installs_xfce_in_base_packages():
    """The whole reason we restructured: GUI bits moved out of the
    base recipe so `default` is thin."""
    recipe = (REPO_ROOT / "image" / "fleetboot-base.yaml").read_text()
    # The string `- xfce4` appears in the base mmdebstrap list if XFCE
    # snuck back in.
    assert "\n      - xfce4\n" not in recipe
    assert "\n      - lightdm\n" not in recipe


def test_base_recipe_defaults_to_multi_user_target():
    """Without a desktop profile, the image must NOT try to start
    graphical.target — `Requires=display-manager.service` would fail."""
    recipe = (REPO_ROOT / "image" / "fleetboot-base.yaml").read_text()
    assert "systemctl set-default multi-user.target" in recipe
    assert "systemctl set-default graphical.target" not in recipe


def test_base_recipe_sources_resolved_profile_dir():
    """The recipe must use the resolver's output, not raw profiles/."""
    recipe = (REPO_ROOT / "image" / "fleetboot-base.yaml").read_text()
    assert "./profiles_resolved/" in recipe
    assert "./profiles/{{" not in recipe  # template var version of raw path
