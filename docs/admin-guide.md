# Fleetboot — administrator's guide

For a school IT admin (or anyone running a managed-desktop fleet) standing
up Fleetboot from scratch on their own infrastructure. Walks through the
full lifecycle: install, image build, network config, enrolment,
day-to-day operations.

Cross-references:
- Architecture and threat model — `DESIGN.md`.
- TFTP server internals — `https://github.com/your-org/tftpjail`.

---

## What you're deploying

```
   ┌─────────────────────┐
   │  fleetboot server   │   one Linux host (Debian / Ubuntu)
   │  ┌───────────────┐  │   - tftpjail listens on UDP/69
   │  │   tftpjail    │  │   - fleetboot listens on HTTPS (your port)
   │  │   fleetboot   │  │   - SQLite registry
   │  │   registry    │  │
   │  └───────────────┘  │
   └─────────┬───────────┘
             │  L2 — your school LAN
   ┌─────────┴───────────┐
   │  the fleet          │   each client:
   │   x86_64   arm64    │   - UEFI machine, no disk needed
   │   ─────    ─────    │   - empty disk OR boots from network only
   │   ─────    ─────    │   - identical image every boot
   └─────────────────────┘
```

The fleetboot server is one well-known Linux host. Each client boots from
the network, fetches a read-only squashfs into RAM, and presents a
graphical login that's backed by FreeIPA.

You'll also run:

- **DHCP** — either your existing router/DHCP server, configured to advertise
  fleetboot as next-server, OR a small dedicated dnsmasq.
- **FreeIPA** — central identity (users, groups: admin / teacher /
  headmaster / student). Either a separate VM or co-located with fleetboot.
- **NFSv4 (Kerberos-secured)** — for `/home/<user>`. Typically on the
  FreeIPA server or a dedicated NAS.

---

## Pre-flight

You need:

- **A Linux server** for fleetboot itself. Modest spec: 2 vCPU, 4 GB RAM,
  ~100 GB disk for built images and registry. Debian 13 (trixie) or later.
- **A DHCP server** you control. School routers often let you set
  bootp options; if yours doesn't, set up a small dnsmasq alongside
  fleetboot.
- **Central identity + storage** — only if you want central login and
  per-user NFS homes. This is the separate `fleetid` project (a FreeIPA
  server + a Kerberos `sec=krb5p` NFSv4 server exporting `/export/home`),
  which you stand up alongside fleetboot. See "Identity + storage" below.
  A fleet that doesn't need central login skips this entirely.

Hardware on the clients:

- UEFI firmware (BIOS-only machines are not supported by this project).
- Network boot enabled in firmware (look for "PXE Boot" / "Network Boot"
  / "Network Stack" in the firmware setup).
- A wired Ethernet port. WiFi PXE is theoretically possible but not
  supported.
- Secure Boot can be left enabled if you're using signed Debian shim+grub
  (one of the deferred items — see "Roadmap" at the end).

---

## Installing fleetboot and tftpjail

On the fleetboot server, as `root`:

```sh
# Dependencies
apt install python3 python3-fastapi python3-httpx uvicorn \
            debos grub-common qemu-utils mksquashfs

# Get the code
mkdir -p /opt/fleetboot && cd /opt/fleetboot
git clone https://github.com/your-org/fleetboot.git
git clone https://github.com/your-org/tftpjail.git
```

Grant the Python interpreter permission to bind UDP/69 without root
(tftpjail is the only thing that needs it):

```sh
setcap cap_net_bind_service=+ep $(readlink -f $(which python3))
```

Run the test gates once to confirm the environment is healthy:

```sh
cd /opt/fleetboot/fleetboot && make test
cd /opt/fleetboot/tftpjail  && make test
```

Both should report all tests passing.

---

## Building the first image

The image is built once and served to every machine that boots. The
image is entirely defined by **composable profiles**; only generic
examples ship upstream. The most important is:

| Profile | What's in it |
|---------|--------------|
| `default` | A very thin Debian base with the fleetboot reporter — no desktop, no GUI, no graphics drivers, and (deliberately) no identity. Every other profile roots here. |

The rest are worked examples you fork or ignore: desktops
(`cinnamon-desktop`, `xfce-desktop`, `gnome-desktop`, `kde-desktop`),
graphics stacks (`intel-graphics`, `amd-graphics`, `nvidia-graphics`),
and helpers (`ssh-debug`, `logo`). Central login + Kerberos NFS homes
come from the separate `fleetid` project's `freeipa` profile (see
"Identity + storage" below) — the base image ships none of it.

To build a desktop image:

```sh
cd /opt/fleetboot/fleetboot
make image PROFILE=cinnamon-desktop
make grub-binary
```

Outputs land in `build/`:

```
build/fleetboot-cinnamon-desktop-amd64.squashfs   # the rootfs machines boot into
build/vmlinuz                                      # kernel
build/initrd.img                                   # initrd, includes live-boot
build/fleetboot-x64-uefi                           # the chainload GRUB binary
```

To define your own deployment — e.g. a `classroom` profile — create a
directory under `image/profiles/` whose `parent` file lists the fragments
to compose (a desktop, `freeipa`, `logo`, …). See `image/profiles/README.md`
for the contract.

You can also customise on top of any profile *without* editing the
project tree — drop your packages, files, or hook scripts into
`image/custom/` (see `image/custom/README.md`). These apply after the
profile, so admin choices win.

---

## DHCP — pointing clients at fleetboot

Whatever DHCP server your network already runs, you need to advertise:

- **DHCP option 54 (next-server)** = the fleetboot server's LAN IP.
- **DHCP option 67 (bootfile-name)** = `fleetboot-x64-uefi` for the
  unsigned chain (Secure Boot **disabled** on every client), or
  `fleetboot-x64-uefi-signed` for the signed chain (Secure Boot **enabled**;
  needs `make signed-boot-assets` once on the build host — see "Signed
  Secure Boot" below).
- **DHCP option 93 (Client System Architecture)** — clients send this;
  configure your DHCP to set a different bootfile per arch when you
  start serving arm64.

### Example: ISC dhcp-server

```
option arch code 93 = unsigned integer 16;
class "bios-x86" {
    match if option arch = 00:00;
    next-server 10.0.0.10;            # fleetboot server LAN IP
    filename "fleetboot-x86-bios";
}
class "uefi-x64" {
    match if option arch = 00:07;
    next-server 10.0.0.10;
    filename "fleetboot-x64-uefi";
}
class "uefi-arm64" {
    match if option arch = 00:0b;
    next-server 10.0.0.10;
    filename "grubnetaa64.efi";       # not yet built; placeholder
}
```

### Example: dnsmasq

```
dhcp-match=set:bios,option:client-arch,0
dhcp-match=set:efi64,option:client-arch,7
dhcp-match=set:efiarm64,option:client-arch,11
dhcp-boot=tag:bios,fleetboot-x86-bios,fleetboot,10.0.0.10
dhcp-boot=tag:efi64,fleetboot-x64-uefi,fleetboot,10.0.0.10
dhcp-boot=tag:efiarm64,grubnetaa64.efi,fleetboot,10.0.0.10
```

### Why arch dispatch matters

DHCP option 93 ("client system architecture") tells us what kind of
firmware just sent the request:

| Value (decimal) | Firmware                  | bootfile                  |
|-----------------|---------------------------|---------------------------|
| 0               | Intel x86PC / legacy BIOS | `fleetboot-x86-bios`      |
| 7               | EFI x86-64                | `fleetboot-x64-uefi`      |
| 9               | EFI BC                    | `fleetboot-x64-uefi`      |
| 11              | EFI ARM 64                | (arm64 build TBD)         |

Without arch dispatch, all clients get the same filename — and a legacy
BIOS machine fed a UEFI binary won't execute it (downloads succeed,
firmware silently drops, PXE loops). The signature in the BOOTP capture
is `ARCH (93), length 2: 0`.

For legacy BIOS clients you also need to set the BOOTP `siaddr` header
field (= `next-server` in ISC dhcpd, = the third positional argument in
the dnsmasq snippet above). Option 66 (TFTP server name) alone isn't
enough — most BIOS PXE stacks ignore it and require `siaddr`.

### Verifying

Once DHCP is configured, plug in a machine and reboot. The UEFI screen
should show:

```
>>Start PXE over IPv4.
  Station IP address is 10.0.0.50
  Server IP address is 10.0.0.10
  NBP filename is fleetboot-x64-uefi
  NBP filesize is 720000 Bytes
  Downloading NBP file...
  NBP file downloaded successfully.
```

If you see "PXE-E16: No valid offer received", DHCP isn't advertising
the bootp options correctly — re-check `next-server` and `filename`.

---

## Signed Secure Boot (optional)

Out of the box, fleetboot ships a self-built `fleetboot-x64-uefi` that has to
run with Secure Boot **off** in each client's firmware. To use Debian's
signed shim + signed grub chain instead — which firmware accepts even
with Secure Boot **on** — install the packages once on the build host:

```sh
apt install shim-signed grub-efi-amd64-signed
```

Then build the signed-boot assets alongside your image:

```sh
make signed-boot-assets   # produces build/fleetboot-x64-uefi-signed, grubx64.efi, grub/grub.cfg
```

The DHCP configuration changes one line — `filename "fleetboot-x64-uefi-signed";`
instead of `filename "fleetboot-x64-uefi";`. The signed grub looks for
`grub/grub.cfg` next to where it was loaded; that file (our
`image/signed-boot/initial-grub.cfg`) just hands control to tftpjail's
per-MAC config exactly as the unsigned binary does.

## Identity + storage: the `fleetid` project

Central login (FreeIPA) and per-user Kerberos NFSv4 home directories are
**not part of fleetboot**. They are a separate, opt-in project,
[`fleetid`](https://github.com/fleetboot/fleetid), cloned alongside this
repo. fleetboot's base image is identity-agnostic; you add identity by:

1. **Standing up the servers.** fleetid ships a FreeIPA identity server
   and a Kerberos NFSv4 home server, either as host scripts
   (`fleetid/server/`) or a `docker compose` stack (`fleetid/compose/`).
   Full walkthrough in fleetid's `README.md` and `DESIGN.md`.

2. **Composing the `freeipa` image profile.** fleetid ships a composable
   Fleetboot profile that adds the FreeIPA / SSSD / Kerberos / NFS packages,
   the first-boot enrolment + keytab-fetch units, and the `krb5p` autofs
   maps. Make it visible to the build and list it in a deployment profile's
   parent chain:

   ```sh
   ln -s ../../../fleetid/profile/freeipa image/profiles/freeipa
   # image/profiles/classroom/parent:
   #   cinnamon-desktop
   #   freeipa
   make image PROFILE=classroom
   ```

3. **Telling the image where the realm is.** Drop a deployment-wide
   `identity.conf` into the admin overlay
   (`image/custom/overlay/etc/fleetboot/identity.conf`):

   ```
   IPA_REALM=SCHOOL.EXAMPLE
   IPA_DOMAIN=school.example
   IPA_SERVER=ipa.school.example       # optional, auto-discovered if blank
   IPA_KEYTAB=/etc/fleetboot/enrol.keytab
   IPA_NFS_SERVER=nfs.school.example
   ```

### What fleetboot does for identity

fleetboot's only role in the identity flow is the two narrow seams fleetid
uses — it "just happens to use fleetboot to register machines":

- **Registering machines.** A machine boots only if it has a row in the
  fleetboot registry (see "Enrolling machines into the registry" below).
  fleetid keys each machine's enrolment keytab by that same MAC.
- **Delivering the enrolment keytab.** fleetid's
  `fleetid/server/ipa-prepare-host.sh <mac>` mints a one-time keytab and
  drops it in fleetboot's keytabs dir. On first boot the image's
  `fetch-keytab` helper pulls it from fleetboot's per-boot-token
  `/enrol/<token>/keytab` endpoint, and `ipa-client-install` consumes it.
  Unprovisioned MACs get a 404 and skip the step silently.

Everything else — the realm, KDC, LDAP, NFS exports, users and groups, the
`admin` / `teacher` / `headmaster` / `student` levels, and the shared-mount
layout — is fleetid's. See fleetid's docs for standing up test users,
resolving the IPA server from clients, and the `sec=krb5p` export layout.

## Running fleetboot and tftpjail

Production deployments use systemd units. A bare-bones `fleetboot.service`:

```
[Unit]
Description=Fleetboot control plane
After=network-online.target

[Service]
WorkingDirectory=/opt/fleetboot/fleetboot
Environment=FLEETBOOT_MINT_SECRET=…long-random-string…
Environment=FLEETBOOT_ADMIN_SECRET=…another-long-random-string…
Environment=FLEETBOOT_BOOT_DIR=/opt/fleetboot/fleetboot/build
Environment=FLEETBOOT_DB_PATH=/var/lib/fleetboot/machines.sqlite
ExecStart=/usr/bin/python3 -m uvicorn \
  fleetboot.server.app:create_app \
  --factory \
  --host 0.0.0.0 \
  --port 443 \
  --ssl-keyfile=/etc/ssl/private/fleetboot.key \
  --ssl-certfile=/etc/ssl/certs/fleetboot.crt
Restart=on-failure
User=fleetboot
Group=fleetboot

[Install]
WantedBy=multi-user.target
```

Generate the two secrets once:

```sh
openssl rand -hex 32 > /etc/fleetboot/mint.secret
openssl rand -hex 32 > /etc/fleetboot/admin.secret
chown root:root /etc/fleetboot/*.secret
chmod 600 /etc/fleetboot/*.secret
```

A `tftpjail.service` runs the UDP/69 server with the same registry by
pointing it at fleetboot's HTTP URL plus the mint secret. The tftpjail
README and `docs/integration.md` cover the wire-up.

`systemctl daemon-reload && systemctl enable --now fleetboot tftpjail`.

---

## FreeIPA enrolment of a machine

When the fleetid `freeipa` profile is layered, the image carries the
`freeipa-client` package and the `fleetboot-freeipa-enroll.service`
oneshot, gated on `/etc/fleetboot/identity.conf`. On first boot it runs
`ipa-client-install --unattended` exactly once (idempotent thereafter).
The one-time keytab is delivered per-MAC via fleetid's
`ipa-prepare-host.sh` + fleetboot's `/enrol/<token>/keytab` rail — see
"Identity + storage: the `fleetid` project" above for the full flow.

The `admin` / `teacher` / `headmaster` / `student` user *levels* are
FreeIPA groups; map them to OS privileges with `sudo`, `polkit`, and
PAM rules — never via the fleetboot profile, which only picks the
**image**, not the **user**.

---

## Enrolling machines into the registry

Every machine that's allowed to boot has a row in the fleetboot machines
registry. Unregistered MACs get default-deny (see DESIGN.md, "Jail
invariants"). Add a machine:

```sh
ADMIN=$(cat /etc/fleetboot/admin.secret)

curl -X POST https://fleetboot.school.example/machines \
  -H "Authorization: Bearer $ADMIN" \
  -H "Content-Type: application/json" \
  -d '{
    "mac": "aa:bb:cc:dd:ee:ff",
    "profile_name": "school",
    "architecture": "x86_64",
    "platform": "efi"
  }'
```

The `profile_name` tells tftpjail which squashfs to point this MAC at
(`fleetboot-school-amd64.squashfs` in this case). To move a machine to a
different profile, re-POST with the new value — `INSERT OR REPLACE`
updates the row in place.

For headless / debug hardware that needs serial console on the kernel
cmdline, add `"serial_console": true`. Don't set this on student
desktops — adds noise to the system journal for no benefit on real
hardware.

To see all enrolled machines:

```sh
curl https://fleetboot.school.example/machines \
  -H "Authorization: Bearer $ADMIN" | jq
```

To remove a machine:

```sh
curl -X DELETE https://fleetboot.school.example/machines/aa:bb:cc:dd:ee:ff \
  -H "Authorization: Bearer $ADMIN"
```

---

## Day-to-day operations

### Adding a teacher / student / headmaster

Pure FreeIPA. `ipa user-add`, then `ipa group-add-member` for the
appropriate group. The OS-side group → privilege wiring is in the
image; see DESIGN.md.

### Adding a new computer

1. Note its MAC address (BIOS/UEFI setup or a label on the chassis).
2. Plug it in.
3. Enrol via the `/machines` API (above).
4. Boot the machine. It PXE-boots, lands at the FreeIPA login.

### Pushing a new image

1. Edit the recipe / profile.
2. `make image PROFILE=…`.
3. The next time each machine reboots, it pulls the new squashfs. No
   per-machine action needed.

For staged rollouts, build the new image with a different profile name
(e.g. `school-pilot`), move a few MACs to that profile, validate, then
flip the rest.

### Watching what the fleet is doing

The status reporter inside each image POSTs lifecycle events
(`network_up`, `nfs_mounted`, `login_ready`, `user_logged_in`) to
fleetboot's `/status` endpoint. fleetboot keeps these per-boot in the
session store. A simple operational dashboard is on the roadmap; for
now, the data is queryable via the FastAPI app's introspection or by
talking to the sqlite registry directly.

---

## Power control

Every machine can be switched **on**, **off** or **rebooted** from its
dashboard page, from the MCP tools (`power_machine`, `power_fleet`), or
by the delete+reboot recovery path. Each action resolves the same way:

1. **The machine's own command** for that action, if one is set —
   `power_on_command`, `power_off_command`, `reboot_command`. Free-text
   shell, run on the fleetboot host with `shell=True`. Use it for
   hardware pdudaemon doesn't cover (IPMI, a cloud API, a wake-on-LAN
   tool).
2. **The fleet-wide pdudaemon.** Set `pdudaemon_host` once under
   *Settings* (e.g. `prowl:16421`), give each machine an alias, and
   fleetboot fires:

   ```
   curl --fail "http://prowl:16421/power/control/<on|off|reboot>?alias=<alias>"
   ```

`--fail` matters: without it curl exits 0 on a "404 unknown alias"
body, and a refusal would masquerade as success.

Reboot has a third route the others can't have — if both fail, the
soft-reboot signal is armed and the machine reboots itself on its next
heartbeat. **On and off have no such fallback**: a machine that is off
can't be asked to do anything, and the reporter has no soft poweroff.
An unwired machine reports failure rather than silently doing nothing.

### Aliases

The alias defaults to the machine's reported hostname, which only
exists once it has booted at least once. For anything you intend to
**power on**, set the alias explicitly — on the machine's dashboard
page under *Power control → Wiring*, or via the `set_pdu_alias` MCP
tool. A machine that has never booted is exactly the one you need to
switch on, and it has no hostname to fall back to.

Power wiring survives re-enrolment: changing a machine's profile won't
throw away how it is powered.

### Using the fleet as on-demand compute

Because the image is immutable and nothing persists on the machine,
powering hardware down costs nothing to undo — what comes back is
identical to what you switched off. That makes a fleetboot fleet usable
as a burst pool: enrol machines on a worker profile, wire their power,
and switch on as many as the work needs:

```
{"name": "power_fleet", "arguments":
 {"action": "on", "profile": "render-node", "count": 8}}
```

Selection is idempotent — `on` only targets machines that aren't
already online, `off` and `reboot` only ones that are — so asking twice
for eight workers doesn't cycle the eight you already have. The reply
reports `succeeded` and `short_by` (how many the fleet couldn't
supply). Poll `list_machines` for `online` and `state` to see which
ones are ready. Full walkthrough: `describe_fleetboot` topic
`on-demand-compute`.

There is no scheduler and no queueing here: fleetboot switches machines
on and tells you when they're up. What runs on them is your workload's
business.

---

## Driving fleetboot from an AI agent (MCP)

The control plane speaks the [Model Context
Protocol](https://modelcontextprotocol.io) at `POST /mcp`, so an LLM
client (Claude Code, Claude Desktop, anything that speaks MCP) can do
anything the dashboard can: enrol machines, author profiles, start
builds, roll a new image out to a whole class of machines, reboot a
wedged host, tail boot events. Auth is a bearer token — the same admin
secret that opens the dashboard.

```
claude mcp add --transport http fleetboot http://<fleetboot-host>:8080/mcp \
    --header "Authorization: Bearer <admin secret>"
```

The endpoint **documents itself**. An agent that connects gets a
briefing at handshake time, and can then call `describe_fleetboot` for
full documentation with worked examples:

| Topic | Covers |
|-------|--------|
| `overview` | What fleetboot is, the four layers, what it deliberately isn't |
| `services` | Every service on offer — MCP tools, the HTTP rails machines boot against, the operator API |
| `profiles` | The profile contract and how inheritance merges packages, overlays and scripts |
| `workflows` | Recipes: enrol, roll out an image, diagnose, reboot, retire |
| `boot-lifecycle` | Each boot state, who emits it, and what a stall there means |
| `examples` | Raw JSON-RPC: client config, handshake, tool calls, error shapes |

The same pages are served as MCP resources (`fleetboot://docs/<topic>`)
for clients that read resources, and rendered for humans at
`/dashboard/mcp` alongside the live tool catalogue. Because the docs
are generated from the same module the server runs, the tool list on
that page can't drift from the tools that actually exist.

Two things to know before pointing an agent at a live fleet:

- **The bearer token is full admin.** There is no read-only MCP scope.
  An agent that can list machines can also delete them.
- **Nothing is versioned.** Profile saves overwrite files in place and
  `delete_machine` drops the row. The `workflows` topic tells the agent
  this too, but it's worth knowing yourself.

---

## Troubleshooting

| Symptom | What to check |
|---------|---------------|
| Client boots to "PXE-E16: No valid offer received" | DHCP isn't advertising bootp options. Re-check `next-server` and `filename`. |
| "PXE-E99 Unexpected network error" or "NBP filesize is 0 Bytes" | tftpjail's OACK handling needs to be on. If you're running an old tftpjail, update. |
| `Unable to find a live file system on the network` | live-boot couldn't fetch the squashfs. Check the kernel cmdline includes a valid `fetch=` URL (visible on the serial console with `serial_console: true`), and that fleetboot's `/boot/...` returns 200 for that URL. |
| `error: out of memory` from GRUB | The rendered grub.cfg is fetching kernel/initrd over HTTP. tftpjail should serve them over TFTP — confirm `grub_template.py` uses `(tftp,${pxe_default_server})/vmlinuz`. |
| Unknown MAC tries to boot | This is intentional default-deny. Enrol the MAC, or build the lockdown/registration image and enrol it under that profile. |

For a real fleet, run a `tcpdump -i any port 67 or port 69` on the
fleetboot server while a problem client boots — that, plus the client's
serial console (`serial_console: true` in the registry), gives you almost
everything.

---

## Roadmap / what's deferred

These are real gaps. None block initial deployment but each is on the
list:

- **Signed shim + grub for Secure Boot.** Today we serve a self-built
  unsigned `fleetboot-x64-uefi`. Secure Boot must be off in firmware. Using
  Debian's signed shim chain is documented in DESIGN.md but not yet
  packaged in the build.
- **VLAN segmentation for per-role network policy.** Currently
  *deferred*; intended to attach DNS blocklists and the egress firewall
  per profile. See DESIGN.md.
- **Operational dashboard.** A web UI on top of the fleetboot API that
  surfaces enrolled machines, recent boot events, and current boot
  states.

---

## When you get stuck

- `DESIGN.md` — explains *why* the architecture is the shape it is. If a
  decision feels wrong, the rationale is probably written down there.
- `image/profiles/README.md` — the profile contract, for adding new
  variants.
- `image/custom/README.md` — the admin-customisation contract, for
  layering site-local changes on top of any profile.
- `make functional-test` — runs the entire control-plane wire (fleetboot
  + tftpjail) in-process, fast. If it passes locally, the protocol
  is healthy.
- `make qemu-functional-test` — boots a UEFI guest through the full PXE
  chain end-to-end. Slow but definitive: if it passes, real hardware
  should too.
