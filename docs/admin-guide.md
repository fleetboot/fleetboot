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
build/grubnetx64.efi                     # the chainload GRUB binary
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
- **DHCP option 67 (bootfile-name)** = `grubnetx64.efi`.
- **DHCP option 93 (Client System Architecture)** — clients send this;
  configure your DHCP to set a different bootfile per arch when you
  start serving arm64.

### Example: ISC dhcp-server

```
option arch code 93 = unsigned integer 16;
class "uefi-x64" {
    match if option arch = 00:07;
    next-server 10.0.0.10;            # fleetboot server LAN IP
    filename "grubnetx64.efi";
}
class "uefi-arm64" {
    match if option arch = 00:0b;
    next-server 10.0.0.10;
    filename "grubnetaa64.efi";
}
```

### Example: dnsmasq

```
dhcp-match=set:efi64,option:client-arch,7
dhcp-match=set:efiarm64,option:client-arch,11
dhcp-boot=tag:efi64,grubnetx64.efi,fleetboot,10.0.0.10
dhcp-boot=tag:efiarm64,grubnetaa64.efi,fleetboot,10.0.0.10
```

### Verifying

Once DHCP is configured, plug in a machine and reboot. The UEFI screen
should show:

```
>>Start PXE over IPv4.
  Station IP address is 10.0.0.50
  Server IP address is 10.0.0.10
  NBP filename is grubnetx64.efi
  NBP filesize is 720000 Bytes
  Downloading NBP file...
  NBP file downloaded successfully.
```

If you see "PXE-E16: No valid offer received", DHCP isn't advertising
the bootp options correctly — re-check `next-server` and `filename`.

---

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
  unsigned `grubnetx64.efi`. Secure Boot must be off in firmware. Using
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
