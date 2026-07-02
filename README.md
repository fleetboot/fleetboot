# fleetboot

A netboot fleet control plane. Boots heterogeneous (x86_64 / arm64,
UEFI / BIOS) machines into an immutable Debian image over PXE,
tracks their lifecycle from GRUB to login-screen, and gives an admin
a small web dashboard to enrol machines, roll out new images, and
reboot / recover misbehaving hardware.

The image the fleet boots is entirely defined by composable profiles,
so the same control plane can drive very different deployments:
locked-down desktop labs, headless compute nodes, dev VM farms, CI
runners, kiosks, whatever your profile chain assembles. A worked
example of the "school desktop" case lives under
`image/profiles/school/` — treat it as one recipe among many, not
the intended use.

## Architecture

Four layers, kept as separable as they can be:

1. **Boot / provisioning** — [`tftpjail`](https://github.com/fleetboot/tftpjail)
   serves the initial GRUB binary and a per-MAC `grub.cfg` over TFTP.
   Every request is authorised against fleetboot's registry, and
   asserted MACs are cross-checked against ARP so a client can't
   impersonate its neighbour.
2. **Image build** — `debos` recipe under `image/` produces a
   `.squashfs` root + kernel + initrd. Layered "profiles" compose
   with inheritance so a target image is just the union of its
   ancestors: an `intel-graphics` profile ships Mesa drivers, a
   `cinnamon-desktop` profile ships the DE, a `logo` profile ships
   a greeter wallpaper, a hypothetical `compute-node` profile
   might ship only ssh + a CUDA runtime. Bring your own profiles.
3. **Control plane** — a FastAPI server (`fleetboot/server/`) that
   holds the machine registry, mints per-boot session tokens for
   tftpjail, receives lifecycle reports from the booted machines,
   and renders the operator dashboard.
4. **In-image reporter** — a small Python package (`fleetboot/reporter/`)
   installed into every built image. systemd units report lifecycle
   states (`network_up`, `nfs_mounted`, `login_console`, ...) and
   the reporter also collects hardware inventory + diagnostics on
   every heartbeat.

Boot lifecycle visibility starts *before* the kernel is up: the
per-MAC grub.cfg emits `grub_running`, `kernel_loaded`,
`initrd_loaded`, `booting_kernel` via TFTP so the dashboard sees the
PXE chain unfolding in real time.

## Getting started

Requires `debos`, `fakemachine`, `python3`, and a checkout of
[`tftpjail`](https://github.com/fleetboot/tftpjail) next to this repo.

```
# Build an image (any profile you have defined works here)
make image PROFILE=default ARCH=amd64

# Run the control plane + tftpjail (single process, dev mode)
make run-server

# Point a machine or VM at DHCP + browse http://localhost:8080/dashboard
```

The dashboard walks you through enrolling a machine, defining
auto-enrol rules (by MAC prefix or IP CIDR), and triggering builds.

## Repository layout

- `fleetboot/server/` — FastAPI app + Jinja2 dashboard
- `fleetboot/reporter/` — in-image reporter Python package
- `fleetboot/tftp_glue/` — the fleetboot-specific side of the
  tftpjail integration (grub config renderer, HTTP client, TFTP-routed
  grub-event intercept callback)
- `image/` — debos recipe, systemd units, runtime helpers,
  base-overlay, and profile tree
- `image/profiles/` — composable profile fragments (extra-packages,
  overlay tree, setup-chroot, parent chain)
- `tests/` — the whole thing has to pass `make test` before every
  change. Test count is over 370 and growing.

## Companion projects

- [`tftpjail`](https://github.com/fleetboot/tftpjail) — the
  jailed TFTP server. Deliberately generic; fleetboot's specific
  integration lives in this repo (`fleetboot/tftp_glue/`).

## Design details

See `DESIGN.md` for the full architecture writeup, `docs/admin-guide.md`
for operational notes, and each profile's `README.md` for
per-profile conventions.

## Development

`make test` runs the whole suite. `make image` builds a full image
(size depends on the profile chain — a headless base takes a couple
of minutes, a full desktop 10–15). `make run-server` brings up the
control plane against `build/dev/machines.sqlite` for interactive
testing. The dashboard is bound to `0.0.0.0:8080` and gated by a
per-install admin secret (see `build/dev/secrets.env`).
