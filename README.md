# fleetboot

A netboot fleet control plane for schools. Boots heterogeneous
(x86_64 / arm64, UEFI / BIOS) machines into a locked-down, immutable
Debian desktop over PXE, tracks their lifecycle from GRUB to
login-screen, and gives an admin a small web dashboard to enrol
machines, roll out new images, and reboot / recover misbehaving
hardware.

## Architecture

Four layers, kept as separable as they can be:

1. **Boot / provisioning** — [`tftpjail`](https://github.com/fleetboot/tftpjail)
   serves the initial GRUB binary and a per-MAC `grub.cfg` over TFTP.
   Every request is authorised against fleetboot's registry, and
   asserted MACs are cross-checked against ARP so a client can't
   impersonate its neighbour.
2. **Image build** — `debos` recipe under `image/` produces a
   `.squashfs` root + kernel + initrd. Layered "profiles" compose
   with inheritance (a `logo` profile ships a wallpaper for the
   greeter, an `intel-graphics` profile ships Mesa drivers, a
   `cinnamon-desktop` profile ships the DE, etc.).
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
# Build a desktop image
make image PROFILE=cinnamon-desktop ARCH=amd64

# Run the control plane + tftpjail (single process, dev mode)
make run-server

# Point a VM at DHCP + browse http://localhost:8080/dashboard
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

`make test` runs the whole suite. `make image` builds a full
desktop image (10–15 minutes). `make run-server` brings up the
control plane against `build/dev/machines.sqlite` for interactive
testing. The dashboard is bound to `0.0.0.0:8080` and gated by a
per-install admin secret (see `build/dev/secrets.env`).
