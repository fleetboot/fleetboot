"""Tests for scripts/resolve-profile.py.

The resolver is the single source of truth for "what does it mean for
profile X to inherit from Y": chain order, package dedup, overlay
shadowing, setup-chroot concatenation. Each rule has a test.
"""

from __future__ import annotations

import importlib.util
import shutil
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
RESOLVER_PATH = REPO_ROOT / "scripts" / "resolve-profile.py"


def _load_resolver():
    spec = importlib.util.spec_from_file_location(
        "resolve_profile_module", RESOLVER_PATH,
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def resolver(monkeypatch, tmp_path):
    """Resolver pointed at a tmp profiles_dir we can scaffold per-test."""
    module = _load_resolver()
    profiles_dir = tmp_path / "profiles"
    profiles_dir.mkdir()
    monkeypatch.setattr(module, "PROFILES_DIR", profiles_dir)
    return module, profiles_dir, tmp_path / "resolved"


def _write_profile(
    profiles_dir: Path,
    name: str,
    *,
    parents: list[str] | None = None,
    packages: list[str] | None = None,
    setup_chroot: str | None = None,
    overlay_files: dict[str, str] | None = None,
):
    """Tiny helper for scaffolding a profile in tests."""
    p = profiles_dir / name
    p.mkdir()
    if parents is not None:
        (p / "parent").write_text("\n".join(parents) + "\n")
    if packages is not None:
        (p / "extra-packages.list").write_text(
            "\n".join(packages) + "\n"
        )
    if setup_chroot is not None:
        (p / "setup-chroot").write_text(setup_chroot)
    if overlay_files:
        for rel, content in overlay_files.items():
            target = p / "overlay" / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)


# ---- chain resolution ----------------------------------------------------


def test_root_profile_resolves_to_self(resolver):
    module, profiles_dir, _ = resolver
    _write_profile(profiles_dir, "base")
    assert module.resolve_chain("base") == ["base"]


def test_single_parent_chain(resolver):
    module, profiles_dir, _ = resolver
    _write_profile(profiles_dir, "base")
    _write_profile(profiles_dir, "child", parents=["base"])
    assert module.resolve_chain("child") == ["base", "child"]


def test_multi_parent_in_declared_order(resolver):
    """When a profile lists two parents, the resolver visits them
    left-to-right; the chain reflects that order."""
    module, profiles_dir, _ = resolver
    _write_profile(profiles_dir, "alpha")
    _write_profile(profiles_dir, "beta")
    _write_profile(profiles_dir, "combo", parents=["alpha", "beta"])
    assert module.resolve_chain("combo") == ["alpha", "beta", "combo"]


def test_diamond_dedupes_shared_ancestor(resolver):
    """A shared base appears only once in the linearised chain."""
    module, profiles_dir, _ = resolver
    _write_profile(profiles_dir, "base")
    _write_profile(profiles_dir, "left", parents=["base"])
    _write_profile(profiles_dir, "right", parents=["base"])
    _write_profile(profiles_dir, "leaf", parents=["left", "right"])
    chain = module.resolve_chain("leaf")
    assert chain == ["base", "left", "right", "leaf"]
    # And `base` MUST appear exactly once — diamond resolved.
    assert chain.count("base") == 1


def test_missing_parent_raises(resolver):
    module, profiles_dir, _ = resolver
    _write_profile(profiles_dir, "child", parents=["does-not-exist"])
    with pytest.raises(module.ResolutionError, match="profile not found"):
        module.resolve_chain("child")


def test_cycle_raises(resolver):
    module, profiles_dir, _ = resolver
    _write_profile(profiles_dir, "a", parents=["b"])
    _write_profile(profiles_dir, "b", parents=["a"])
    with pytest.raises(module.ResolutionError, match="cycle"):
        module.resolve_chain("a")


def test_parent_file_skips_comments_and_blanks(resolver):
    module, profiles_dir, _ = resolver
    _write_profile(profiles_dir, "base")
    p = profiles_dir / "child"
    p.mkdir()
    (p / "parent").write_text(
        "# comment\n\nbase\n   \n# another comment\n"
    )
    assert module.resolve_chain("child") == ["base", "child"]


# ---- merging packages -----------------------------------------------------


def test_package_dedup_keeps_first_occurrence(resolver):
    module, profiles_dir, out_dir = resolver
    _write_profile(profiles_dir, "base", packages=["vim", "curl"])
    _write_profile(profiles_dir, "child", parents=["base"],
                   packages=["curl", "git"])
    module.resolve("child", out_dir)
    text = (out_dir / "child" / "extra-packages.list").read_text()
    lines = [line for line in text.splitlines() if line.strip()]
    assert lines == ["vim", "curl", "git"]


def test_package_comments_are_stripped(resolver):
    module, profiles_dir, out_dir = resolver
    _write_profile(profiles_dir, "base", packages=[
        "# comment", "", "vim", "  ", "# another", "curl"
    ])
    module.resolve("base", out_dir)
    text = (out_dir / "base" / "extra-packages.list").read_text()
    assert "comment" not in text
    lines = [line for line in text.splitlines() if line.strip()]
    assert lines == ["vim", "curl"]


# ---- merging overlays -----------------------------------------------------


def test_child_overlay_shadows_parent(resolver):
    module, profiles_dir, out_dir = resolver
    _write_profile(profiles_dir, "base", overlay_files={
        "etc/motd": "from base\n",
        "etc/hostname": "base-host\n",
    })
    _write_profile(profiles_dir, "child", parents=["base"],
                   overlay_files={"etc/motd": "from child\n"})
    module.resolve("child", out_dir)
    motd = (out_dir / "child" / "overlay" / "etc" / "motd").read_text()
    hostname = (out_dir / "child" / "overlay" / "etc" / "hostname").read_text()
    assert motd == "from child\n"     # child wins
    assert hostname == "base-host\n"  # not redefined, parent keeps it


# ---- combined setup-chroot ------------------------------------------------


def test_setup_chroots_concatenated_in_order(resolver):
    module, profiles_dir, out_dir = resolver
    _write_profile(profiles_dir, "base", setup_chroot=(
        "#!/bin/sh\necho base step\n"
    ))
    _write_profile(profiles_dir, "child", parents=["base"], setup_chroot=(
        "#!/bin/sh\necho child step\n"
    ))
    module.resolve("child", out_dir)
    combined = (out_dir / "child" / "setup-chroot").read_text()
    # Both bodies appear, base then child.
    assert "echo base step" in combined
    assert "echo child step" in combined
    assert combined.index("base step") < combined.index("child step")
    # Only one shebang at the top — per-script shebangs were stripped.
    assert combined.count("#!/bin/sh") == 1
    # Per-profile delimiters present.
    assert "=== from profile: base ===" in combined
    assert "=== from profile: child ===" in combined


# ---- real-profile integration ---------------------------------------------


def test_real_school_profile_resolves_with_xfce_desktop_parent(tmp_path):
    """The shipped `school` profile must successfully resolve and include
    its desktop ancestor's packages."""
    module = _load_resolver()
    out = module.resolve("school", tmp_path)
    chain = (out / "resolved-from").read_text().strip()
    assert "xfce-desktop -> school" in chain
    packages = (out / "extra-packages.list").read_text()
    # School's own contribution.
    assert "extrepo" in packages
    # XFCE-desktop ancestor's contribution.
    assert "xfce4" in packages
    assert "lightdm" in packages


def test_real_default_profile_resolves_to_just_itself(tmp_path):
    """`default` is the thin base — no parent, no extra packages."""
    module = _load_resolver()
    out = module.resolve("default", tmp_path)
    chain = (out / "resolved-from").read_text().strip()
    assert chain == "default"
    packages = (out / "extra-packages.list").read_text().strip()
    # Comments and blank lines are stripped; nothing real to install.
    assert packages == ""


def test_resolve_outputs_resolved_from_sidecar(resolver):
    """The resolver writes the resolved chain to a sidecar file so
    builds can be audited from the output dir alone."""
    module, profiles_dir, out_dir = resolver
    _write_profile(profiles_dir, "base")
    _write_profile(profiles_dir, "child", parents=["base"])
    module.resolve("child", out_dir)
    sidecar = (out_dir / "child" / "resolved-from").read_text()
    assert sidecar.strip() == "base -> child"
