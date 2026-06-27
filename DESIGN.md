# OpenSchool — System Design

OpenSchool automates and controls computers across a school network. Machines
netboot a locked-down, immutable Debian desktop with central identity and
NFS-mounted home directories, and their internet access is filtered. Security is
the top priority throughout.

Assumptions about the fleet:

- Every client is UEFI and netboot-capable.
- Machines are heterogeneous and may be **x86_64 or arm64**.
- The PXE network is **flat** (clients and the boot server share one broadcast
  domain). VLAN segmentation is out of scope for now.

---

## The four layers

1. **Boot / provisioning** — gets each machine from power-on to a running kernel,
   choosing what it may boot by MAC and MAC group. Implemented by the standalone
   **tftpjail** project (see below).
2. **Desktop image** — an immutable, read-only Debian desktop delivered as a
   squashfs root with a tmpfs overlay. This *is* the lockdown surface.
3. **Identity & storage** — FreeIPA (Kerberos + LDAP + DNS) for central accounts,
   the `admin` / `teacher` / `headmaster` / `student` levels, and Kerberos-secured
   NFSv4 home directories.
4. **Network control** — filtering DNS resolver (blocklists) plus an egress
   firewall to stop students reaching anything not granted.

---

## Locked decisions

| Area | Decision | Why |
|------|----------|-----|
| Identity / NFS | **FreeIPA** (Kerberos + LDAP + DNS) | Kerberos-secured NFSv4 out of the box; without it NFS is network-spoofable. |
| Root filesystem | **squashfs over HTTP + tmpfs overlay** | Immutable, scalable, fresh every boot — no writable system surface to attack. |
| Control-plane language | **Python (FastAPI)** | Readable for school maintainers; fast to build. |
| Network bootloader | **Signed GRUB EFI** (shim → grub), not iPXE | Works on x86_64 **and** arm64, keeps Secure Boot intact, scriptable. |
| Boot filtering | **tftpjail** — custom TFTP server | All boot policy (MAC, group, lockdown) lives in code we control. |
| First milestone | One MAC boots to desktop, end-to-end, proven in a QEMU UEFI guest. | Thinnest vertical slice that proves the whole spine. |

**GRUB version:** target a build new enough that **HTTP** and the **`smbios`**
command both work — i.e. GRUB **≥ 2.06** (Debian 12 *bookworm*), preferring
**2.12** (Debian 13 *trixie*). The network GRUB image must include the `efinet`,
`tftp`, `http`, and `smbios` modules.

---

## Boot spine (end to end)

1. **DHCP (not ours)** hands the client an IP, `next-server = tftpjail`, and a
   bootfile name selected by **DHCP option 93** (Client System Architecture Type).
   This is the DHCP layer's *only* job — a mechanical arch → filename mapping, no
   policy. It guarantees the right-arch initial binary even for unknown machines.
2. **tftpjail serves the arch-matched signed GRUB binary.** Arch is read straight
   from the requested filename. This binary is the public Debian-signed
   shim → grub chain: it carries **no secrets and no authority**, so serving it by
   arch to any host is safe and necessary (an unregistered machine still needs to
   boot *something* to reach a registration screen).
3. **GRUB's embedded first-stage config** gathers what the client knows about
   itself — `$net_default_mac`, `$grub_cpu`, `$grub_platform`, and (via `smbios`)
   the system UUID — and requests a path that encodes them:
   `/jail/<mac>/<arch>/<platform>/<uuid>`. The requested path *is* the uplink
   channel (TFTP is read-only; over HTTP a query string works equally well).
4. **tftpjail resolves identity and applies the jail.** It parses the asserted
   MAC, cross-checks it against the ARP-resolved MAC of the request source IP,
   looks up the registry (MAC / MAC group → profile), and either:
   - **default-denies** (unknown or unauthorized) → registration / lockdown
     config, or
   - **renders a machine-specific `grub.cfg` on the fly** (kernel, initrd,
     squashfs URL, kernel cmdline).
5. **GRUB loads kernel + initrd** → boots the immutable Debian image → tmpfs
   overlay, FreeIPA enrollment, Kerberos NFSv4 `/home` mount.

---

## tftpjail — the boot-policy brain

A standalone project: a TFTP server where **every read request is authorized
against a per-client policy** ("a jail per host"). Stock TFTP servers are open
file servers — any host reads any file — which would leak every profile's configs
and image paths to any student. tftpjail exists to fix exactly that.

### Identity

GRUB reports its own MAC, arch, platform, and UUID by encoding them in the
request path. This is **client-asserted** and therefore spoofable, so:

- Treat the GRUB-reported MAC as a **routing hint**, and **cross-check it against
  the ARP / neighbour-table MAC** for the request's source IP (reliable on a flat
  L2). Log mismatches.
- **Boot profile ≠ privilege.** A MAC/profile only decides *which image and
  network policy* a machine gets — never *who you are*. Real identity and the
  user levels come from FreeIPA/Kerberos login at the OS layer, which a boot path
  cannot spoof. Keep all secrets out of boot assets.

Identity tuple used for policy: `(mac, arch, platform, uuid?)`.

### Jail invariants (each one becomes a test)

- **Default-deny.** Unknown MAC or ungranted path → TFTP error, zero bytes.
- **Jailed namespace.** A client resolves only files its profile authorizes;
  another profile's config is denied even though it exists.
- **No enumeration oracle.** "Unauthorized" and "not found" return the *same*
  error, so probing can't map which profiles/files exist.
- **No writes, ever.** WRQ is always refused — removes a whole attack class.
- **No path traversal.** `..`, absolute escapes, and NUL bytes are rejected
  before any lookup.
- **Configs are rendered per-profile, not stored as shared files** — there is no
  flat directory of grub configs to grab.
- **Asserted-MAC vs ARP-MAC consistency check** on every authorized request.

### Arch handling, settled

- *First binary:* arch comes from the DHCP-set (option 93) filename — works for
  every machine, including unknown ones. No ARP or DHCP sniffing needed for arch.
- *After the first binary:* GRUB self-reports `$grub_cpu`, so every later stage is
  arch-correct regardless.
- ARP is therefore used **only** as the anti-spoof MAC cross-check, not for arch.

---

## Identity, user levels, and authorization

- Accounts and the `admin` / `teacher` / `headmaster` / `student` levels live in
  **FreeIPA** as users and groups.
- The desktop image pulls them via PAM/nsswitch (SSSD).
- **Privilege is enforced at the OS layer** by group membership → `polkit`,
  `sudo`, and PAM rules — never by the boot profile.
- `/home` is **NFSv4 with Kerberos (krb5p)**, mounted per-user at login. Without
  Kerberos, any plugged-in laptop could read every home directory.

---

## The desktop image — build and customisation

The image is built with **debos** (Debian OS builder, packaged in trixie) using
a single recipe at `image/openschool-base.yaml`. debos runs every step
inside a `fakemachine` (a lightweight QEMU VM), so the build is host-independent
and reproducible, and it can build **arm64 on an x86_64 host** via
`qemu-user-static`.

The base recipe produces:

- `build/openschool-<arch>.squashfs` — the read-only root,
- `build/vmlinuz` and `build/initrd.img` — extracted from the image so they can
  be served by tftpjail for netboot. The initrd includes **live-boot**, which
  knows how to fetch the squashfs over HTTP and set up the tmpfs overlay.

### Admin customisation contract

OpenSchool ships one base recipe; admins **never edit it**. Instead they
customise via four stable contract points under `image/custom/`:

| Contract point | Purpose |
|----------------|---------|
| `extra-packages.list` | One apt package per line; merged into the base install. |
| `overlay/` | Files copied verbatim into the image root. |
| `hooks/pre-build`, `hooks/post-build` | Optional shell scripts run on the host at fixed moments. |
| `local.yaml` | Optional debos snippet for extension actions. |

This is documented for administrators in `image/custom/README.md`. The structural
tests under `make test` assert the recipe still consumes every contract point —
so a refactor that breaks the contract can't land silently.

### Build / test split

| Target | What | Speed | When |
|--------|------|-------|------|
| `make test` | Fast: code unit tests + recipe structural tests + customisation-contract checks. **No real build, no QEMU.** | < 1s | every change (the hard gate) |
| `make image` | Run debos. Produces squashfs + kernel + initrd. | minutes | on demand / before a deploy |
| `make image-smoke` | Boot the built image in QEMU UEFI (OVMF) and assert the in-image reporter posts `network_up` to a stub server. | minutes | nightly / pre-release |

The smoke test fetches the squashfs over HTTP using **live-boot's `fetch=` mode**
— deliberately the same path the real netboot will use, so the smoke covers the
actual production wire format, not a contrived disk-mount.

---

## Lockdown model

The read-only squashfs root + tmpfs overlay means **there is no writable system
surface and nothing on the box that isn't in the image**. Students get a pristine
system every boot; changes never persist outside their Kerberos-protected home.
"Cannot access anything not added to our netboot images" is enforced structurally,
not by policing a writable system.

Internet control sits on top: a filtering DNS resolver (Unbound/BIND with RPZ
blocklists) plus an egress firewall. DNS policy attaches to a network segment, so
per-role differentiation would eventually want per-role VLANs — **deferred** with
VLAN support.

---

## Testing strategy (hard gate)

- **Unit tests** for all code we write — tftpjail's protocol parsing, identity
  resolution, jail policy, and config rendering especially.
- **Integration tests** that boot **real UEFI guests in QEMU** against the live
  stack and assert they reach the desktop with the correct profile and mounts.
- A single **`make test`** runs both tiers and is wired into CI. Every change runs
  the full suite — see `CLAUDE.md`.

---

## Project layout

- **`openschool`** — umbrella: image build, FreeIPA, NFS, DNS/network control,
  integration tests.
- **`tftpjail`** — separate project: the boot-policy TFTP brain.

Proposed `tftpjail` first slice: `protocol` + `identity` + `policy` (default-deny
core) with full unit tests — the security spine — before wiring the live UDP loop.

---

## Deferred / out of scope (for now)

- **VLAN support** — shelved. (Would later be a switch-enforced identity signal
  and the natural home for per-role DNS/egress policy.)
- **DHCP option-93 sniffing** — unnecessary; DHCP encodes arch in the bootfile
  name directly.
- **Passive DHCP snoop for unknown-machine arch** — not needed for the same
  reason.
