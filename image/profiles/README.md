# Image profiles

Each subdirectory of `profiles/` is a self-contained image variant.
Profiles compose with inheritance — a profile declares zero or more
parents in its `parent` file, and the resolver produces the union of
every ancestor's contributions (see `scripts/resolve-profile.py` for
the exact semantics).

Fleetboot ships two kinds of profiles as examples:

- **`default`** — a very basic Debian base with the fleetboot
  reporter installed. No desktop, no display manager, no graphics
  drivers. Every other profile should inherit from something
  eventually rooted here.
- **Desktop / graphics examples** — `cinnamon-desktop`,
  `xfce-desktop`, `gnome-desktop`, `kde-desktop` plus
  `intel-graphics`, `amd-graphics`, `nvidia-graphics`, `ssh-debug`,
  `logo`. These are worked examples of how to compose a full
  desktop image out of small orthogonal profiles. Use them as-is,
  fork them, or ignore them entirely — the resolver treats every
  profile the same.

## Layout

```
profiles/
  <name>/
    parent                # newline-separated parent profile names   (optional)
    extra-packages.list   # apt packages to install on top of base   (optional)
    overlay/              # files copied verbatim into image root    (optional)
    setup-chroot          # shell script run inside the chroot       (optional)
    suite                 # Debian release codename (default trixie) (optional)
    README.md             # what this profile is for                 (recommended)
```

All files are optional. A profile with only a `README.md` is a no-op
(equivalent to whatever its parents contribute — or the base image
if there are none).

`setup-chroot` runs inside the chroot **after** the base apt install,
so it can call `apt-get` to add third-party repositories (e.g. via
`extrepo`), register additional package sources, or run any
one-shot configuration. When multiple ancestors have a `setup-chroot`
they get concatenated in inheritance order with per-profile
delimiters, so the build log makes it clear which ancestor's block
ran.

## Build a specific profile

```sh
make image                              # default (very thin base)
make image PROFILE=cinnamon-desktop     # ships the example desktop chain
make image PROFILE=my-fleet ARCH=arm64  # your own profile
```

The output filename is `build/fleetboot-<profile>-<arch>.squashfs`.

## Profile vs. `image/custom/`

| | `profiles/<name>/` | `image/custom/` |
|--|------|------|
| Maintained by | the fleetboot project (`default`) or the deployer | the local admin |
| Selected by | `PROFILE=` at build time | always applied |
| Composes order | applied first | applied **last**, so admin choices override |

A deployer who wants one of the shipped desktop examples plus their
own overlays builds `make image PROFILE=cinnamon-desktop` while
their site-specific tweaks sit in `image/custom/`. A deployer with
their own profile tree replaces / adds under `profiles/<name>/` and
selects with `PROFILE=<name>`.
