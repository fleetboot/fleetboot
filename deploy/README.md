# Deploying fleetboot on a new machine

Deployment is **docker compose**, the same path used for local
development. Two containers are the minimum viable fleet:

| Container | What it does | Network |
|-----------|--------------|---------|
| `fleetboot` | Control plane, dashboard, MCP endpoint, boot-asset HTTP | publishes `${FLEETBOOT_PORT:-8080}` |
| `tftpjail` | Jailed TFTP server — hands each machine its GRUB binary and per-MAC config | host networking, UDP 69 |

**Both are required.** A fleetboot without tftpjail serves boot assets
nothing can ask for: the PXE chain starts at TFTP, and tftpjail is what
authorises a MAC and renders its `grub.cfg`. They ship in the same
`docker-compose.yml` for that reason.

Optionally, the [`fleetid`](https://github.com/fleetboot/fleetid)
identity stack (FreeIPA + Kerberos NFSv4 homes) runs alongside — see
[Full stack with identity](#full-stack-with-identity) below.

---

## Prerequisites on the host

- **Docker Engine + the compose plugin** (`docker compose version` ≥
  2.20 — the full-stack example uses `include:`).
- **UDP port 69 free.** Debian/Ubuntu often ship `tftpd-hpa`; if it is
  installed it will fight tftpjail for the port:

  ```sh
  systemctl disable --now tftpd-hpa
  ```

- **A stable LAN IP**, because it gets baked into every rendered
  `grub.cfg` as `CLIENT_BASE_URL`. A machine that PXEs cannot ask DNS
  what changed.
- **DHCP you control**, advertising this host as `next-server` with an
  arch-appropriate bootfile name. fleetboot does not run DHCP — see
  `docs/admin-guide.md` for ISC and dnsmasq examples.
- **`/dev/kvm` and the `kvm` group**, *only* if this host will also
  build images. The dashboard's build button shells out to `debos`,
  which boots a KVM guest; the compose file passes the device, the
  group, and read-only `/lib/modules` + `/boot` through for it. A
  control-plane-only box can drop those from the service — builds then
  happen elsewhere and you sync the artefacts into `build/`.

---

## Install

fleetboot and tftpjail are separate projects, and the tftpjail image is
built from **both** trees (the generic TFTP server plus fleetboot's
`tftp_glue`). So the checkouts must be siblings:

```sh
mkdir -p /opt/fleetboot && cd /opt/fleetboot
git clone https://github.com/fleetboot/fleetboot.git
git clone https://github.com/fleetboot/tftpjail.git
cd fleetboot
```

Configure secrets and the client-facing URL:

```sh
cp .env.example .env
chmod 600 .env          # it holds the admin secret in plain text
$EDITOR .env
```

At minimum set `FLEETBOOT_ADMIN_SECRET`, `FLEETBOOT_MINT_SECRET` (any
two long random strings — `openssl rand -hex 32` twice), and
`CLIENT_BASE_URL` pointing at this host's LAN IP and port.

Bring it up:

```sh
docker compose up -d --build
```

Verify:

```sh
curl -fsS http://localhost:8080/healthz && echo          # {"ok":true}
docker compose ps                                        # both healthy/up
ss -lunp | grep :69                                      # tftpjail has the port
```

Then browse to `http://<host>:8080/dashboard` and log in with username
`admin` and your `FLEETBOOT_ADMIN_SECRET`.

Build the first image (on a host with KVM):

```sh
make image PROFILE=default ARCH=amd64
```

Artefacts land in `build/`, which is bind-mounted into both containers,
so they are picked up with no restart.

---

## Running as a service

`restart: unless-stopped` plus an enabled `docker.service` already
brings the stack back after a reboot. If you want the deployment
itself supervised — so `systemctl status fleetboot` is meaningful and
the stack is ordered against the network — drop in a unit:

```ini
# /etc/systemd/system/fleetboot.service
[Unit]
Description=fleetboot control plane (docker compose)
Requires=docker.service
After=docker.service network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=/opt/fleetboot/fleetboot
ExecStart=/usr/bin/docker compose up -d --remove-orphans
ExecStop=/usr/bin/docker compose down
TimeoutStartSec=0

[Install]
WantedBy=multi-user.target
```

```sh
systemctl daemon-reload && systemctl enable --now fleetboot
```

Use `WorkingDirectory=/opt/fleetboot/fleetboot/deploy/full-stack` for
the identity stack instead.

---

## Full stack with identity

`deploy/full-stack/compose.yml` composes this repo's compose file with
fleetid's, using compose `include:` so each project keeps its own
relative paths and its own `.env`. Clone fleetid as a third sibling:

```sh
cd /opt/fleetboot && git clone https://github.com/fleetboot/fleetid.git
cd fleetboot/deploy/full-stack
cp ../../../fleetid/compose/.env.example ../../../fleetid/compose/.env
$EDITOR ../../../fleetid/compose/.env
docker compose up -d --build              # fleetboot + tftpjail
docker compose --profile ipa up -d ipa    # FreeIPA (5-10 min first run)
docker compose --profile nfs up -d nfs    # Kerberos NFSv4 homes
```

Set `FLEETID_DIR` if the checkout is somewhere other than a sibling
directory.

The identity services sit behind compose profiles deliberately: FreeIPA
claims host ports 443, 389, 88 and 464, and the NFS container is
privileged with host networking. Neither should start by accident.
Bring `ipa` up first and let its install finish before `nfs`, which
needs a keytab minted from it. fleetid's own README covers realm setup,
the keytab, and provisioning home directories — this file only gets the
containers running.

Two host-level constraints that compose cannot enforce:

- **The host must not also run `nfs-kernel-server`.** There is one
  kernel nfsd; the container needs it.
- **FreeIPA needs forward *and* reverse DNS** for `IPA_SERVER_HOSTNAME`
  before first start. On a flat LAN without a zone for the realm, that
  means `/etc/hosts` entries on the server and on every client (the
  `freeipa-desktop` profile ships exactly that).

---

## Operating it

**Upgrading.** Pull and rebuild; the registry migrates itself on open
(new columns are added idempotently at startup), so there is no
separate migration step:

```sh
cd /opt/fleetboot/fleetboot && git pull
cd ../tftpjail && git pull
cd ../fleetboot && docker compose up -d --build
```

Booted machines are unaffected while the control plane restarts — they
only talk to it on heartbeats. To roll a new *image* out to the fleet,
build with `auto_reboot`, or arm reboots from the dashboard.

**What holds state.**

| Where | What | Backup |
|-------|------|--------|
| volume `fleetboot-data` | SQLite machine registry + boot sessions | `docker run --rm -v fleetboot_fleetboot-data:/d -v $PWD:/out alpine tar czf /out/registry.tgz -C /d .` |
| `build/` in the checkout | kernels, initrds, squashfs images | rebuildable from profiles; several GB |
| `image/profiles/` | profile definitions, bind-mounted **read-write** | it is a git tree — commit dashboard edits |
| volume `freeipa-data` | the entire FreeIPA installation | `docker compose exec ipa ipa-backup` — never tar it live |

The registry is the only thing that is genuinely irreplaceable, and
it's small. Back it up.

**Logs.** `docker compose logs -f fleetboot tftpjail`. Set
`FLEETBOOT_LOG_LEVEL=debug` / `TFTPJAIL_LOG_LEVEL=debug` in `.env` for
the boot path in detail.

**TLS.** The dashboard, the admin REST API and the MCP endpoint all
authenticate with the same secret in a header, in plain HTTP by
default. On anything but a trusted admin LAN, drop a cert and key into
`tls/` and set `FLEETBOOT_TLS_CERT` / `FLEETBOOT_TLS_KEY` to their
in-container paths (`/tls/<file>`). Note that `CLIENT_BASE_URL` must
then be `https://…`, and every booting machine has to trust the CA.

**Exposure.** Publish 8080 only where you need it. The boot network is
untrusted by design — tftpjail default-denies unknown MACs — but the
dashboard and MCP endpoint are full fleet control behind one shared
secret, so treat that port as an admin interface.
