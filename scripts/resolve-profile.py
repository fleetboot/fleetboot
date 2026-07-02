#!/usr/bin/env python3
"""Resolve a profile + its ancestors into a single staged build dir.

Profiles can declare one or more parents via a `parent` file:

  image/profiles/my-fleet/parent
  ----------------------------------------------
  cinnamon-desktop
  amd-graphics
  ----------------------------------------------

Multiple parents are listed one per line and resolved in order. Comments
(`#`) and blank lines are ignored. A diamond — two ancestors that share a
common parent — sees that common parent contribute exactly once.

For each profile in the resolved chain (root first, target last) the
contribution is:

  - extra-packages.list: appended, then de-duplicated keeping the first
    occurrence. Comment and blank lines are stripped.
  - overlay/: copied over the merged tree. Child overlays shadow parents
    (no deletion semantics; a child can only add or replace).
  - setup-chroot: concatenated, each ancestor's section delimited with
    a `# === from profile: <name> ===` marker so build logs are legible.

Output: image/profiles_resolved/<target-profile-name>/

The output dir is build-time-only; gitignored.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PROFILES_DIR = REPO_ROOT / "image" / "profiles"
DEFAULT_OUTPUT_DIR = REPO_ROOT / "image" / "profiles_resolved"


class ResolutionError(ValueError):
    """Raised on cycles, missing parents, or other resolver-level errors."""


def _read_parents(profile_name: str) -> list[str]:
    """Return the list of parents declared by `profile_name` (empty if none)."""
    parent_file = PROFILES_DIR / profile_name / "parent"
    if not parent_file.is_file():
        return []
    parents: list[str] = []
    for line in parent_file.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parents.append(stripped)
    return parents


def resolve_chain(profile_name: str) -> list[str]:
    """Return the linearised resolution order (root-first, target last).

    Depth-first walk of parents; each profile appears exactly once.
    Cycles raise ``ResolutionError``.
    """
    chain: list[str] = []
    seen: set[str] = set()
    visiting: set[str] = set()

    def visit(name: str) -> None:
        if name in seen:
            return
        if name in visiting:
            raise ResolutionError(
                f"cycle detected while resolving '{profile_name}': "
                f"'{name}' visited recursively"
            )
        if not (PROFILES_DIR / name).is_dir():
            raise ResolutionError(
                f"profile not found: '{name}' "
                f"(expected at {PROFILES_DIR / name})"
            )
        visiting.add(name)
        for parent in _read_parents(name):
            visit(parent)
        visiting.remove(name)
        if name not in seen:
            chain.append(name)
            seen.add(name)

    visit(profile_name)
    return chain


def _merge_extra_packages(chain: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for ancestor in chain:
        path = PROFILES_DIR / ancestor / "extra-packages.list"
        if not path.is_file():
            continue
        for line in path.read_text().splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if stripped in seen:
                continue
            seen.add(stripped)
            out.append(stripped)
    return out


def _merge_overlays(chain: list[str], dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    for ancestor in chain:
        src = PROFILES_DIR / ancestor / "overlay"
        if not src.is_dir():
            continue
        shutil.copytree(src, dest, dirs_exist_ok=True)


def _combine_setup_chroots(chain: list[str], dest: Path) -> None:
    """Concatenate each ancestor's setup-chroot into a single script.

    We strip the per-script shebang lines so the combined script's
    single `#!/bin/sh` is the only one. Each section is wrapped in a
    delimiter so build logs make it clear which profile's setup ran.
    """
    parts: list[str] = ["#!/bin/sh", "set -eu", ""]
    have_any = False
    for ancestor in chain:
        path = PROFILES_DIR / ancestor / "setup-chroot"
        if not path.is_file():
            continue
        have_any = True
        content = path.read_text()
        # Drop the shebang if present.
        lines = content.splitlines()
        if lines and lines[0].startswith("#!"):
            lines = lines[1:]
        parts.append(f"# === from profile: {ancestor} ===")
        parts.extend(lines)
        parts.append("")
    if not have_any:
        # Empty but executable — keep the recipe simple.
        parts.append("# (no setup-chroot in any ancestor)")
    dest.write_text("\n".join(parts))
    dest.chmod(0o755)


def resolve(profile_name: str, output_dir: Path) -> Path:
    """Write the resolved profile under ``output_dir/<profile_name>/``."""
    chain = resolve_chain(profile_name)

    out = output_dir / profile_name
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    # extra-packages.list
    packages = _merge_extra_packages(chain)
    (out / "extra-packages.list").write_text(
        "\n".join(packages) + ("\n" if packages else "")
    )

    # overlay/
    _merge_overlays(chain, out / "overlay")

    # setup-chroot
    _combine_setup_chroots(chain, out / "setup-chroot")

    # Sidecar so admins (and tests) can confirm the chain.
    (out / "resolved-from").write_text(" -> ".join(chain) + "\n")

    return out


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("profile", help="profile name to resolve")
    parser.add_argument(
        "--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR,
        help=f"output directory (default: {DEFAULT_OUTPUT_DIR})",
    )
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = _parse_args(argv)
    try:
        out = resolve(args.profile, args.output_dir)
    except ResolutionError as err:
        print(f"resolve-profile: {err}", file=sys.stderr)
        return 1
    print(f"resolved -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
