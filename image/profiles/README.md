# Image profiles

Each subdirectory of `profiles/` is a self-contained image variant that we
ship alongside the base recipe. A profile lets us produce more than one
flavour of fleetboot image from the same base — for example, a generic
`default` image vs. a `school` image with extra apps.

## Layout

```
profiles/
  <name>/
    extra-packages.list   # apt packages to install on top of the base (optional)
    overlay/              # files copied verbatim into the image root  (optional)
    setup-chroot          # shell script run inside the chroot         (optional)
    README.md             # what this profile is for                   (recommended)
```

All four files are optional. A profile with only a `README.md` is a no-op
(equivalent to the base image).

`setup-chroot` runs inside the chroot **after** the base apt install, so it
can call `apt-get` to add third-party repositories (e.g. via `extrepo`),
register additional package sources, or run any one-shot configuration.

## Build a specific profile

```sh
make image                   # default profile
make image PROFILE=school    # school profile
make image PROFILE=lab ARCH=arm64
```

The output filename is `build/fleetboot-<profile>-<arch>.squashfs`.

## Profile vs. `image/custom/`

| | `profiles/<name>/` | `image/custom/` |
|--|------|------|
| Maintained by | the fleetboot project | the local admin |
| Selected by | `PROFILE=` at build time | always applied |
| Composes order | applied first | applied **last**, so admin choices override |

A school admin who wants the `school` profile with their org-specific tweaks
builds `make image PROFILE=school` while their tweaks sit in `image/custom/`.
