# fleetboot

A netboot fleet control plane. Boots heterogeneous (x86_64 / arm64,
UEFI / BIOS) machines into an immutable Debian image over PXE,
tracks their lifecycle from GRUB to login-screen, and gives an admin
a small web dashboard to enrol machines, roll out new images, and
reboot / recover misbehaving hardware.

The image the fleet boots is entirely defined by composable profiles,
so the same control plane can drive very different deployments:
locked-down desktop labs, headless compute nodes, dev VM farms, CI
runners, kiosks, whatever your profile chain assembles. Only two
kinds of profile ship upstream:

- `default` — a very thin Debian base with the fleetboot reporter,
  no desktop, no GUI, no graphics drivers. Every other profile
  should ultimately be rooted here.
- A handful of desktop / graphics / helper examples
  (`cinnamon-desktop`, `xfce-desktop`, `gnome-desktop`,
  `kde-desktop`, `intel-graphics`, `amd-graphics`,
  `nvidia-graphics`, `ssh-debug`, `logo`) to show how to compose
  a real image. Fork them, ignore them, or replace them entirely —
  the resolver treats every profile the same.

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

Local dev and production share one deployment path: **docker
compose**. Check out this repo and its sibling
[`tftpjail`](https://github.com/fleetboot/tftpjail):

```
git clone https://github.com/fleetboot/fleetboot.git
git clone https://github.com/fleetboot/tftpjail.git
cd fleetboot
```

Configure a `.env` file with your secrets and the LAN URL clients
will use to reach the control plane, then bring the stack up:

```
cp .env.example .env
# edit .env: set FLEETBOOT_ADMIN_SECRET, FLEETBOOT_MINT_SECRET,
#   and CLIENT_BASE_URL (e.g. http://192.168.1.50:8080)
make run-server
```

That brings up two containers — `fleetboot` (control plane +
dashboard on port 8080) and `tftpjail` (jailed TFTP server on UDP
port 69, host-networked). Point a booted machine at the LAN via DHCP
and browse to `http://localhost:8080/dashboard`.

Image builds still run on the host (they need `debos` + `fakemachine`
with kernel access, which containers don't get):

```
make image PROFILE=default ARCH=amd64
```

Artefacts land in `build/` which is bind-mounted into both containers,
so a new build is immediately picked up without a restart.

The dashboard walks you through enrolling a machine, defining
auto-enrol rules (by MAC prefix or IP CIDR), and triggering builds.

For a real deployment on a new machine — host prerequisites, secrets,
running it as a systemd-supervised service, backups, upgrades — see
[`deploy/README.md`](deploy/README.md). `deploy/full-stack/compose.yml`
is a worked example that brings up the control plane, tftpjail, and the
`fleetid` identity stack (FreeIPA + Kerberos NFSv4 homes) as one
compose project.

## Driving it from an agent

The control plane speaks [MCP](https://modelcontextprotocol.io) at
`POST /mcp`, so an LLM client can do anything the dashboard can —
enrol machines, author profiles, build images, roll one out fleet-wide,
reboot a wedged host, tail boot events:

```
claude mcp add --transport http fleetboot http://<host>:8080/mcp \
    --header "Authorization: Bearer <admin secret>"
```

The endpoint explains itself. `describe_fleetboot` serves fleetboot's
own documentation — what the system is, everything it offers, how
profiles compose, step-by-step recipes, what each boot state means when
a machine stalls, and the raw wire protocol — all with worked examples.
The same pages are MCP resources (`fleetboot://docs/<topic>`) and are
rendered for humans at `/dashboard/mcp`.

## On-demand compute

The same registry that keeps a lab on the right image will run that
hardware as a burst pool. Machines are switched on and off through
pdudaemon (or a per-machine command), and because the root filesystem
is read-only with a tmpfs overlay, powering one down costs nothing to
undo — what comes back is identical to what you switched off:

```
{"name": "power_fleet", "arguments":
 {"action": "on", "profile": "render-node", "count": 8}}
```

`list_machines` reports `online` and how far each boot got, so an agent
can tell when the capacity it asked for is actually ready. There's no
scheduler and no queueing — fleetboot switches machines on and tells
you when they're up.

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
- [`fleetid`](https://github.com/fleetboot/fleetid) — optional
  identity + storage layer: a FreeIPA identity server, a
  Kerberos-secured NFSv4 home-directory server, and a composable
  `freeipa` image profile. The fleetboot base image is
  **identity-agnostic**; add central login + per-user NFS homes by
  standing up fleetid and composing its profile into your image
  chain. fleetid uses fleetboot only to register machines and to
  deliver each machine's enrolment keytab over `/enrol/<token>/keytab`.

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
