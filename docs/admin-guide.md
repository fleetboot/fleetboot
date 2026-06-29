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
- **A FreeIPA installation** (planned separately — out of scope here, but
  see "FreeIPA enrolment" below for the one-time per-machine join).
- **An NFSv4 server** with Kerberos (`sec=krb5p`) exporting `/export/home`.

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

The image is built once and served to every machine that boots. Two
flavours ship out of the box:

| Profile | What's in it |
|---------|--------------|
| `default` | XFCE desktop, FreeIPA-backed login, Kerberos NFS home. No browser. |
| `school`  | Default + LibreWolf set as the system browser. |

To build the school image:

```sh
cd /opt/fleetboot/fleetboot
make image PROFILE=school
make grub-binary
```

Outputs land in `build/`:

```
build/fleetboot-school-amd64.squashfs    # the rootfs students boot into
build/vmlinuz                            # kernel
build/initrd.img                         # initrd, includes live-boot
build/fleetboot-x64-uefi                     # the chainload GRUB binary
```

To define your own profile — e.g. a `lab` profile with engineering tools
— copy the `school/` directory under `image/profiles/` and edit the
`extra-packages.list` / `setup-chroot`. See `image/profiles/README.md`
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

## FreeIPA — identity and authentication

Fleetboot's image authenticates users against FreeIPA via SSSD + LightDM.
FreeIPA owns the LDAP directory, the Kerberos KDC, and (optionally) DNS
for the realm. Each fleetboot machine is itself a Kerberos host
principal so it can mount Kerberos-secured NFS without per-user keytabs.

### Why containers

FreeIPA Server isn't packaged for Debian (only the client). The
upstream FreeIPA project ships a maintained Fedora-based container image,
which is the cleanest path on a Debian host:

```sh
sudo IPA_ADMIN_PASS='ChangeMe123!' IPA_DM_PASS='ChangeMeDmToo!' \
    ./scripts/setup-ipa-server.sh
```

That pulls `freeipa/freeipa-server:fedora-rawhide`, persists its state
under `/var/lib/ipa-data`, and runs the install unattended. Takes
5–10 minutes; watch progress with `docker logs -f freeipa-server`.

The script pre-flight-checks that ports 53, 80, 88, 389, 443, 464,
636, and 749 are all free on the host. If `systemd-resolved` is binding
53, or `slapd`/`bind` is already running, the script bails before
touching anything.

Defaults (override with env vars at the top of the script):

| Variable | Default |
|---|---|
| `IPA_REALM` | `FLEETBOOT.LAN` |
| `IPA_DOMAIN` | `fleetboot.lan` |
| `IPA_SERVER_FQDN` | `ipa.fleetboot.lan` |
| `IPA_DATA_DIR` | `/var/lib/ipa-data` |

### Resolving the IPA server from clients

Both the fleetboot host and the booted images need to resolve
`ipa.fleetboot.lan` to the IPA container's IP. Two options:

- **`/etc/hosts`** entry on each client — fine for a small fleet,
  but it's per-machine state outside fleetboot's view.
- **DNS forwarding** — IPA's own DNS server can be the resolver for
  the realm. On dev (libvirt), add to your libvirt network XML:
  `<dns><forwarder addr='192.168.99.1'/></dns>` so the bridge's
  dnsmasq forwards realm queries to IPA.

### Test users for dev

```sh
sudo IPA_ADMIN_PASS='ChangeMe123!' ./scripts/ipa-add-test-users.sh
```

Creates four accounts (`alice`, `bob` in `students`; `carol` in
`teachers`; `dave` in `headmaster`) with the shared password
`ChangeMe123!` (FreeIPA forces a change on first login).

### Per-host enrolment

For each machine to enrol, mint a keytab and drop it where fleetboot's
`/enrol/<token>/keytab` endpoint can deliver it on first boot:

```sh
sudo IPA_ADMIN_PASS='ChangeMe123!' \
    ./scripts/ipa-prepare-host.sh aa:bb:cc:dd:ee:ff
```

That adds `fleetboot-aabbccddeeff.fleetboot.lan` to IPA, generates a
one-shot enrolment keytab, and writes it to
`/var/lib/fleetboot/keytabs/aa:bb:cc:dd:ee:ff.keytab` (mode 0600).
On first boot the image's `fleetboot-keytab-fetch.service` pulls it
via the per-boot-token-authenticated `/enrol` endpoint and
`ipa-client-install` consumes it.

### identity.conf (per-deployment)

The image needs to know the realm + domain + server FQDN. Drop a
deployment-wide identity.conf into the admin overlay
(`image/custom/overlay/etc/fleetboot/identity.conf`):

```
IPA_REALM=FLEETBOOT.LAN
IPA_DOMAIN=fleetboot.lan
IPA_SERVER=ipa.fleetboot.lan
IPA_KEYTAB=/etc/fleetboot/enrol.keytab
IPA_NFS_SERVER=ipa.fleetboot.lan
```

Then rebuild your profile images:

```sh
make image PROFILE=school
```

The image's `enroll-freeipa` service reads this on first boot and runs
`ipa-client-install` against it.

## NFS server — homes + shared mounts

Fleetboot ships the fleetboot host as the NFS server too: same machine,
same identity-domain, Kerberos all the way down. Two trees get exported:

- `/export/home/<user>` — the user's personal home. Mounted at
  `/home/<user>` per-login via autofs, with `sec=krb5p` (encrypted),
  authenticated by the user's Kerberos ticket. No other user can read it.
- `/export/shared/<bucket>` — shared dirs. Mounted under `/shared/<bucket>`.
  Still Kerberos-secured at the wire level; visibility inside each
  bucket is enforced by **POSIX group permissions**, so you don't need
  a separate export per group.

### One-time server setup

Pre-reqs on the fleetboot host: it's already FreeIPA-enrolled
(`ipa-client-install` ran successfully), and you have an admin Kerberos
ticket (`kinit admin`).

```sh
sudo ./scripts/setup-nfs-server.sh \
    --realm SCHOOL.EXAMPLE \
    --domain school.example
```

The script is idempotent. It:

1. Installs `nfs-kernel-server`, `nfs-common`, `krb5-user`.
2. Creates the IPA service principal `nfs/<host>@REALM` and writes its
   keytab to `/etc/krb5.keytab` (mode 0600).
3. Lays down `/etc/exports` from `nfs/exports.template` — `sec=krb5p`
   everywhere, no `sec=sys` fallback.
4. Renders `/etc/idmapd.conf` from `nfs/idmapd.conf.template`, substituting
   `__IPA_DOMAIN__` so NFSv4 owner strings round-trip to real uid:gid.
5. Creates `/export/{home,shared/all}` with sensible perms.
6. Enables and starts `nfs-kernel-server` and `nfs-idmapd`.

### Lay out shared directories

The shared root exists; the per-bucket directories are an admin curation
step. `nfs/shared-skeleton.md` recommends a starting layout:

| Path                          | Owner / mode                 | Audience |
|-------------------------------|------------------------------|----------|
| `/export/shared/all`          | `root:root` mode `1777`      | All users, sticky. |
| `/export/shared/teachers`     | `root:teachers` mode `2770`  | Teachers only. |
| `/export/shared/headmaster`   | `root:headmaster` mode `2770`| Headmaster only. |
| `/export/shared/students`     | `root:students` mode `2775`  | Students writable, others read-only. |
| `/export/shared/coursework`   | `root:teachers` mode `2775`  | Teachers write; students read. |

The setgid bit on group-owned dirs (mode prefix `2`) means new files
inherit the parent group, which is what you want for shared workflows.

```sh
sudo install -d -o root -g teachers -m 2770 /export/shared/teachers
sudo install -d -o root -g headmaster -m 2770 /export/shared/headmaster
sudo install -d -o root -g students -m 2775 /export/shared/students
sudo install -d -o root -g teachers -m 2775 /export/shared/coursework
```

### identity.conf — what the clients need to know

The image's `enroll-freeipa` substitutes the NFS server's FQDN into
`/etc/auto.home` and `/etc/auto.shared` at first boot, using
`IPA_NFS_SERVER` from `/etc/fleetboot/identity.conf`. So the per-machine
identity config grows one line:

```
IPA_REALM=SCHOOL.EXAMPLE
IPA_DOMAIN=school.example
IPA_KEYTAB=/etc/fleetboot/enrol.keytab
IPA_NFS_SERVER=fleetboot.school.example
```

If you co-locate fleetboot with NFS (the default), `IPA_NFS_SERVER`
is the fleetboot host's own FQDN.

### Per-MAC FreeIPA enrolment keytab delivery

For fleets where each machine should auto-enrol with FreeIPA on first
boot without the admin pre-staging keytabs on every box, fleetboot can
serve them per-MAC via a per-boot-token authenticated endpoint.

On the IPA server, mint a keytab per machine:

```sh
ipa host-add fleetboot-lab-01.school.example
ipa-getkeytab -s ipa.school.example -p host/fleetboot-lab-01.school.example \
              -k /var/lib/fleetboot/keytabs/aa:bb:cc:dd:ee:ff.keytab
```

Copy the file to fleetboot's `keytabs_dir` (e.g. `/var/lib/fleetboot/keytabs/`)
keyed by the machine's MAC. On first boot the image's
`fleetboot-keytab-fetch.service` POSTs `/enrol/<token>/keytab`, writes
the file to `/etc/fleetboot/enrol.keytab` mode 0600, and the
`fleetboot-freeipa-enroll.service` runs `ipa-client-install --unattended`
against it. Unprovisioned MACs get 404 from the endpoint and skip the
fetch step silently.

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

The image already contains the `freeipa-client` package and a one-shot
systemd unit (`fleetboot-freeipa-enroll.service`) gated on the existence
of `/etc/fleetboot/identity.conf`. To enrol a machine on first boot:

1. Create a one-time enrolment principal in FreeIPA (via the web UI or
   `ipa host-add` + `ipa-getkeytab`). You get back a keytab file.
2. Drop it on the machine via a profile-scoped admin overlay (under
   `image/custom/overlay/etc/fleetboot/`) OR — better — serve it from
   fleetboot's API as a per-MAC asset (planned; see Roadmap).
3. Drop `/etc/fleetboot/identity.conf` next to it:
   ```
   IPA_REALM=SCHOOL.EXAMPLE
   IPA_DOMAIN=school.example
   IPA_KEYTAB=/etc/fleetboot/enrol.keytab
   ```
4. Reboot. The enrolment oneshot runs `ipa-client-install --unattended`
   exactly once. After that, `/etc/ipa/default.conf` exists and the unit
   never runs again.

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
- **Per-MAC enrolment keytab delivery via fleetboot's API.** Today, the
  IPA keytab has to land on the machine via an admin overlay. A per-MAC
  fleetboot endpoint that mints + delivers a one-time-use enrolment
  keytab is the natural next step.
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
