# `school` profile

The base fleetboot image (XFCE desktop, FreeIPA-backed identity, Kerberos
NFSv4 home directories, the boot-state reporter) plus **LibreWolf** as the
default web browser. Intended for classroom desktops and student labs.

## Why LibreWolf

LibreWolf is a Firefox fork that ships with telemetry, ads, and tracking
disabled by default, and it has a stable Debian apt repository. For a
locked-down student desktop those defaults are the right ones.

## How it lands in the image

`extra-packages.list` brings in [`extrepo`](https://salsa.debian.org/extrepo-team/extrepo),
a tiny Debian tool that registers third-party apt repositories from a curated
list (including the maintainer-signed key). `setup-chroot` then runs
`extrepo enable librewolf && apt-get install librewolf` inside the image
build, and sets LibreWolf as the default `x-www-browser`.

## Build

```sh
make image PROFILE=school
```

Produces `build/fleetboot-school-amd64.squashfs`.

## Enrol machines into this profile

```
curl -X POST http://fleetboot.example.internal/machines \
  -H "Authorization: Bearer $ADMIN_SECRET" \
  -d '{
    "mac": "aa:bb:cc:dd:ee:ff",
    "profile_name": "school",
    "architecture": "x86_64",
    "platform": "efi"
  }'
```

The `profile_name` here is what tftpjail's renderer stamps into the kernel
cmdline, so the booted machine fetches `fleetboot-school-amd64.squashfs`
instead of the default.
