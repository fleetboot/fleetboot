"""Structural tests for the built-in image profiles.

We do not build the profile images here (debos is too slow for `make test`).
These tests assert the on-disk structure: the contract files exist and have
shapes the recipe relies on.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PROFILES_DIR = REPO_ROOT / "image" / "profiles"


def test_profiles_index_readme_exists():
    """The profiles directory documents the contract for adding new ones."""
    readme = PROFILES_DIR / "README.md"
    assert readme.is_file()
    text = readme.read_text()
    assert "extra-packages.list" in text
    assert "setup-chroot" in text
    assert "PROFILE=" in text


def test_default_profile_exists():
    """The base recipe defaults to PROFILE=default, so it must exist."""
    assert (PROFILES_DIR / "default" / "README.md").is_file()
    # Default's extras list must exist (even if empty) so the recipe's
    # overlay step doesn't choke.
    assert (PROFILES_DIR / "default" / "extra-packages.list").is_file()


def test_default_profile_has_no_extra_real_packages():
    """The default profile must list zero real apt packages on its own; the
    base image's packages cover what the example needs."""
    lines = [
        line.strip()
        for line in (PROFILES_DIR / "default" / "extra-packages.list").read_text().splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert lines == []


def test_school_profile_includes_librewolf():
    """The school profile's setup-chroot installs librewolf via extrepo."""
    extra = PROFILES_DIR / "school" / "extra-packages.list"
    assert extra.is_file()
    pkgs = [
        line.strip()
        for line in extra.read_text().splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    # extrepo is needed *before* librewolf's repository can be enabled.
    assert "extrepo" in pkgs


def test_school_profile_setup_chroot_is_executable_and_installs_librewolf():
    script = PROFILES_DIR / "school" / "setup-chroot"
    assert script.is_file()
    mode = script.stat().st_mode
    assert mode & stat.S_IXUSR, "setup-chroot must be executable"
    contents = script.read_text()
    assert "extrepo enable librewolf" in contents
    assert "apt-get install" in contents
    assert "librewolf" in contents


def test_school_profile_readme_exists():
    readme = PROFILES_DIR / "school" / "README.md"
    assert readme.is_file()
    text = readme.read_text()
    assert "librewolf" in text.lower()
    assert "make image PROFILE=school" in text
