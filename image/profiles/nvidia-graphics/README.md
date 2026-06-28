# `nvidia-graphics` profile

Mix-in profile that adds Debian's proprietary `nvidia-driver` and
DKMS-built kernel module. Like `amd-graphics`, designed to stack with
a desktop profile rather than stand alone.

## Prerequisites

The image's apt sources must include `non-free`. The fleetboot base
recipe enables `main contrib non-free non-free-firmware` by default,
so this works out of the box.

## Stack with a desktop

Create a derived profile:

```
image/profiles/lab-nvidia/parent
    xfce-desktop
    nvidia-graphics
```

```sh
make image PROFILE=lab-nvidia
```

## Caveats

- **Build-host kernel matters** — DKMS rebuilds the module against the
  kernel headers present in the chroot. The image's running kernel
  should match the headers used at build time. Debian images do this
  correctly out of the box; custom kernels need extra care.
- **Secure Boot** — proprietary kernel modules need to be signed with
  a MOK (Machine Owner Key) under Secure Boot. The signed-shim chain
  fleetboot supports doesn't currently wire MOK enrolment; SB must be
  off for nvidia-graphics machines, or enrol your own MOK manually.
- **Older / Pascal cards** may need the `nvidia-tesla-470-driver`
  package instead. Add it to a derived profile if you target that
  hardware.
