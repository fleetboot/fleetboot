"""The documentation fleetboot serves about itself over MCP.

An agent that connects to the MCP endpoint gets a tool catalogue, but a
catalogue only says *how to call* things — not what fleetboot is, what it
can do for you, or what a sensible sequence of calls looks like. This
module holds that missing half: a set of markdown topics exposed two
ways, because MCP clients differ in what they actually surface.

  - As MCP **resources** (`fleetboot://docs/<topic>`) — the protocol's
    native way to hand a client reference material.
  - As the **`describe_fleetboot` tool** — because plenty of clients
    never read resources, and every client can call a tool.

Both surfaces read from `TOPICS` below, so they can't drift apart.
"""

from __future__ import annotations

from dataclasses import dataclass


DOC_URI_PREFIX = "fleetboot://docs/"
DOC_MIME_TYPE = "text/markdown"


@dataclass(frozen=True)
class DocTopic:
    """One documentation page: a name, a one-liner, and the markdown."""

    name: str
    title: str
    summary: str
    body: str

    @property
    def uri(self) -> str:
        return DOC_URI_PREFIX + self.name


_OVERVIEW = """\
# What fleetboot is

fleetboot is a **netboot fleet control plane**. It boots machines over
the network into an immutable Debian image, tracks each machine from
GRUB to login screen, and gives you (or an agent) one place to enrol
hardware, roll out new images, and recover machines that misbehave.

Nothing is installed on the machines. Every boot pulls a read-only
squashfs root over HTTP and layers a tmpfs on top, so a machine's
entire contents are whatever the image build put there — reboot is a
full factory reset, and there is no persistent local state to drift or
be tampered with.

## Two jobs, one registry

**Fleet control** — keep a set of known machines running a known
image. Enrol hardware, roll out a new image to everything on a
profile, watch boots unfold, recover what wedges.

**On-demand compute** — treat that same hardware as a pool you switch
on when there's work and off when there isn't. `power_fleet` takes a
profile and a count; the machines PXE the image that profile builds
and report in when they're ready. Because the image is immutable,
powering a machine off costs nothing to undo — what comes back is
identical to what you switched off, so there is no state to drain, snapshot
or restore. See the `on-demand-compute` topic.

The second is not a different product bolted on; it is what a netboot
fleet with power control already is.

## The four layers

1. **Boot / provisioning** — the `tftpjail` TFTP server hands each
   machine an arch-matched signed GRUB binary, then a `grub.cfg`
   rendered live for that specific MAC. Every request is authorised
   against fleetboot's registry; unknown machines are default-denied.
2. **Image build** — a `debos` recipe turns a *profile* into a
   squashfs + kernel + initrd. Profiles compose by inheritance, so an
   image is the union of its ancestors (see the `profiles` topic).
3. **Control plane** — this server. Machine registry, per-boot session
   tokens, lifecycle reports, build jobs, the operator dashboard, and
   this MCP endpoint.
4. **In-image reporter** — a small agent baked into every image that
   reports lifecycle states and heartbeats hardware inventory and
   diagnostics back to `/status`.

## What it deliberately does not do

- **It is not an identity system.** A boot profile decides *which image
  and network policy* a machine gets — never *who you are*. Central
  login and per-user home directories are the optional companion
  project `fleetid` (FreeIPA + Kerberos-secured NFSv4), which plugs in
  as an image profile plus two seams in this server: registering
  machines, and delivering each machine's enrolment keytab over
  `/enrol/<token>/keytab`.
- **It is not a config-management system.** There is no agent applying
  state to a running machine. If you want a machine to change, you
  change its profile and rebuild — the change lands on next boot.
- **It does not run DHCP.** Your existing DHCP server points clients at
  tftpjail (`next-server`) and picks the boot filename by architecture.
  That is the DHCP layer's only job.

## Vocabulary

- **Machine** — a registry row keyed by MAC. Carries its profile,
  architecture, firmware platform (`efi`/`pc`), scratch mode, last seen
  IP, running image version, and a soft-reboot flag.
- **Profile** — a directory naming extra packages, an overlay tree, a
  setup script, and parent profiles. The unit of image composition.
- **Build** — one run of `make image` for a profile + architecture,
  producing the artefacts machines boot.
- **Boot session** — a short-lived per-boot token that authorises one
  machine to fetch its own boot assets. Not a credential a user holds.
- **Soft reboot** — an armed flag the machine picks up on its next
  heartbeat and acts on itself, used when no power control exists.

## Where to read next

- `services` — everything this server offers, MCP and HTTP.
- `profiles` — how images are composed, and how to author one.
- `workflows` — recipes: enrol, build and roll out, power, diagnose.
- `on-demand-compute` — running the fleet as a burst compute pool.
- `boot-lifecycle` — the states a boot passes through, and what a
  stall at each one usually means.
- `examples` — raw JSON-RPC over the wire, for wiring up a client.
"""


_SERVICES = """\
# What fleetboot offers

## To an agent, over MCP

Everything below is a tool on this endpoint.

| Service | Tools |
|---------|-------|
| Fleet inventory | `list_machines`, `get_machine` |
| Enrolment | `enrol_machine`, `set_machine_profile`, `delete_machine` |
| Power | `power_machine`, `power_fleet`, `reboot_machine`, `set_pending_reboot` |
| Power wiring | `set_power_command`, `set_pdu_alias`, `set_reboot_command` |
| Image authoring | `list_profiles`, `get_profile`, `save_profile`, `write_profile_overlay` |
| Image builds | `start_build`, `list_builds`, `get_build` |
| Observability | `recent_events` |
| Self-documentation | `describe_fleetboot` |

Two of these do fleet-scale work in one call. `start_build` with
`auto_reboot=true` arms the soft-reboot signal on every machine running
that same profile + architecture, so a whole class picks up the new
image on its next heartbeat. `power_fleet` switches a profile's worth
of machines on or off, optionally just `count` of them.

## Power control

Every power action — `on`, `off`, `reboot` — resolves the same way:

1. **The machine's own command** for that action, if set
   (`set_power_command`). Free-text shell, run on the fleetboot host.
   Use it for hardware pdudaemon doesn't cover: IPMI, a cloud API, a
   wake-on-LAN tool.
2. **The fleet-wide pdudaemon**, when the `pdudaemon_host` setting is
   configured and the machine has an alias — `set_pdu_alias`, else its
   reported hostname. Fires
   `curl --fail "<pdudaemon>/power/control/<action>?alias=<alias>"`.

`reboot` has a third route the others can't have: if both fail, the
soft-reboot signal is armed and the machine reboots itself on its next
heartbeat. There is no equivalent for `on` (a machine that is off
cannot be asked to do anything) or `off`, so those report failure
honestly instead of pretending. **Check `ok` on the result** — power
control fails quietly in the real world, and `method` tells you which
route was actually taken.

Set `pdu_alias` at enrolment for anything you intend to power on: the
hostname fallback only exists once a machine has booted and reported
one, and a machine that has never booted is precisely the one you need
to switch on.

## Knowing what's up

`list_machines` and `get_machine` carry a live view alongside the
registry fields: `state` (how far the current boot got — see
`boot-lifecycle`), `last_seen`, `seconds_since_last_seen`, and
`online` (heartbeated within the last 6 minutes). `online` claims no
more than "it spoke to us recently" — from the control plane, a
powered-off machine and a wedged one look the same.

## To machines, over HTTP

These rails are not for you to call — they are the fleet's plumbing —
but knowing them makes diagnosis far easier.

| Endpoint | Who calls it | What it does |
|----------|--------------|--------------|
| `POST /sessions` | tftpjail | Mints a short-lived per-boot token bound to one MAC. Gated by the mint secret. |
| `GET /resolve/{mac}` | tftpjail | Read-only registry lookup so tftpjail can render that machine's `grub.cfg`. Auto-enrol rules fire here for unknown MACs. |
| `GET /boot/{token}/{filename}` | GRUB / the initrd | Serves kernel, initrd and squashfs against a valid boot session. |
| `GET /grub-event/{token}/{state}` | GRUB, over TFTP | Boot-stage events from before the kernel exists, so the dashboard sees the PXE chain unfold live. |
| `POST /status` | the in-image reporter | Lifecycle state, hostname, running image version, hardware inventory, diagnostics. The response carries `pending_reboot` — this is how a soft reboot is delivered. |
| `GET /enrol/{token}/keytab` | the image, at boot | Per-machine FreeIPA enrolment keytab. The `fleetid` seam. |
| `GET /enrol/{token}/authorized_keys` | the image, at boot | Deployment-wide SSH keys, e.g. for the `ssh-debug` profile. |

## To an operator, over HTTP

- `/dashboard` — machines, profiles, builds, events, settings, plus a
  live-rendered copy of this MCP documentation at `/dashboard/mcp`.
- `POST /machines`, `GET /machines`, `PATCH /machines/{mac}` (move a
  machine to another profile), `DELETE /machines/{mac}` and
  `/auto-enrol-rules` — the admin REST API behind the dashboard,
  gated by the same admin secret this MCP endpoint uses.

## Auto-enrolment

Rather than registering every MAC by hand, an auto-enrol rule matches
on a MAC prefix or an IP CIDR and enrols anything that shows up under
it with a chosen profile, architecture, firmware platform, serial
console setting and scratch mode. Unknown machines that match no rule
stay default-denied — they boot nothing.

## Companion projects

- **tftpjail** — the boot-policy brain. Default-deny TFTP, jailed
  per-client namespace, no writes, no enumeration oracle, and an
  asserted-MAC-vs-ARP cross-check on every authorised request.
- **fleetid** — optional identity and storage: FreeIPA for accounts and
  Kerberos, NFSv4 homes mounted `sec=krb5p`, and a composable `freeipa`
  image profile. The fleetboot base image is identity-agnostic; a
  deployment opts in by composing that profile into its chain.
"""


_PROFILES = """\
# Profiles: how an image is composed

A profile is a directory under `image/profiles/<name>/`. Every file in
it is optional, and each has exactly one job:

| File | Effect on the built image |
|------|---------------------------|
| `parent` | Parent profile names, one per line. Resolved in order. |
| `extra-packages.list` | Debian packages to install. Comments and blanks ignored. |
| `overlay/` | A file tree copied onto the image root. `overlay/etc/foo` lands at `/etc/foo`. |
| `setup-chroot` | A shell script run inside the image during the build. |
| `suite` | Debian suite override, if this profile needs a different one. |
| `README.md` | Notes for humans. Not baked into the image. |

## Inheritance

Resolution walks the parent chain root-first, target-last:

- **Packages** are appended, then de-duplicated keeping the first
  occurrence.
- **Overlays** are copied in order, so a child's file shadows its
  parent's. There is no deletion semantics — a child can add or
  replace, never remove.
- **setup-chroot** scripts are concatenated, each section marked with
  `# === from profile: <name> ===` so build logs stay legible.
- A **diamond** (two ancestors sharing a parent) contributes that
  shared ancestor exactly once.

Multiple parents are the norm, not an edge case: a real deployment
profile is usually one line of its own plus a chain like
`cinnamon-desktop` + `intel-graphics` + `freeipa`.

## Authoring one from an agent

`save_profile` creates or updates the directory; `write_profile_overlay`
writes individual files into `overlay/`. Both are idempotent, and
`save_profile` leaves any field you omit unchanged on an existing
profile.

```json
{"name": "save_profile", "arguments": {
  "name": "library-kiosk",
  "parents": ["cinnamon-desktop", "intel-graphics"],
  "extra_packages": "firefox-esr\\n",
  "readme": "# library-kiosk\\n\\nBrowser-only machines by the front desk.\\n"
}}
```

```json
{"name": "write_profile_overlay", "arguments": {
  "name": "library-kiosk",
  "relpath": "etc/fleetboot/kiosk.conf",
  "content": "HOMEPAGE=https://library.example.org\\n"
}}
```

Then build it — see the `workflows` topic.

## Profiles composed in from another project

A profile directory can be a symlink to one another project ships —
that is how fleetid's `freeipa` identity profile is wired in. Two
things follow. `list_profiles` only lists what the server can actually
see, so a symlink whose target isn't reachable simply won't appear.
And `start_build` runs the build inside the control-plane container, so
a build can fail with `profile not found: '<parent>'` even though the
profile is fine on the host — the deployment needs the other project's
tree mounted (`FLEETID_DIR`). If a build fails that way, that mount is
the first thing to check; it is not something you can fix by editing
the profile.

## Conventions worth keeping

- Root every profile at `default` (directly or through ancestors); it
  carries the reporter that makes a machine visible at all.
- Keep one concern per profile. Graphics drivers, desktop environment,
  and deployment specifics compose better as three profiles than one.
- **Keep secrets out of profiles.** An overlay ships to every machine
  running the image, and boot assets are not a secure channel. Machine
  identity comes from the `/enrol` rails at boot, not from the image.
"""


_WORKFLOWS = """\
# Recipes

## Add a machine to the fleet

1. `list_profiles` — see what images exist.
2. `enrol_machine` with the MAC and the profile name. Set
   `architecture` (`x86_64` / `arm64`), `platform` (`efi` / `pc`) and
   `scratch_mode` if the defaults don't fit.
3. Optionally `set_reboot_command` if this box has its own power
   control — a shell command that power-cycles it.
4. Power it on and watch `recent_events` for that MAC.

```json
{"name": "enrol_machine", "arguments": {
  "mac": "54:b2:03:92:33:a9",
  "profile_name": "cinnamon-desktop",
  "architecture": "x86_64",
  "platform": "efi"
}}
```

## Change what a machine boots

`set_machine_profile` moves an enrolled machine onto a different
profile and touches nothing else — architecture, platform, scratch
mode and power wiring all survive:

```json
{"name": "set_machine_profile", "arguments": {
  "mac": "54:b2:03:92:33:a9", "profile_name": "render-node"
}}
```

Use this rather than re-running `enrol_machine`, which replaces the
whole row and resets anything you don't pass. The machine keeps running
its current image until it next boots, so follow with `reboot_machine`
if the change should land now.

The same operation over the admin REST API is
`PATCH /machines/<mac>` with `{"profile_name": "..."}`, and on the
dashboard it's the profile dropdown on a machine's page.

Every route that sets a profile — this tool, `enrol_machine`, the REST
API, the dashboard, and auto-enrol rules — rejects a profile with no
directory. A machine pointed at a profile that doesn't exist boots
nothing, and discovers that at PXE time rather than when you made the
mistake. `list_profiles` tells you what's real.

## Change an image and roll it out to everything running it

1. `get_profile` to read what's there now.
2. `save_profile` and/or `write_profile_overlay` to make the change.
3. `start_build` with `auto_reboot=true`.
4. Poll `get_build` until `state` is `succeeded` or `failed`. On
   success, `auto_reboot_armed` tells you how many machines were swept
   into a pending reboot; they pick the new image up on their next
   heartbeat.

```json
{"name": "start_build", "arguments": {
  "profile": "library-kiosk", "architecture": "amd64",
  "auto_reboot": true
}}
```

Builds are serialised — one at a time — and a full desktop image takes
10–15 minutes, so poll `get_build` on the order of tens of seconds, not
continuously. `get_build` also returns the tail of the build log, which
is where package and `setup-chroot` failures show up.

Note the architecture naming: machines are enrolled with `x86_64` /
`arm64`, builds are started with the Debian names `amd64` / `arm64`.

## Diagnose a machine that isn't coming up

1. `get_machine` — is it enrolled, on the profile you think, and what
   image version is it running?
2. `recent_events` filtered to that MAC — how far did the boot get?
   Compare against the `boot-lifecycle` topic, which says what a stall
   at each state means.
3. If it reached `login_console` but the version is stale, it simply
   needs a reboot to pick up a newer image.

## Switch a machine on or off

```json
{"name": "power_machine", "arguments": {
  "mac": "54:b2:03:92:33:a9", "action": "on"
}}
```

The result tells you which route was taken and whether it worked:

```json
{"mac": "54:b2:03:92:33:a9", "action": "on", "method": "pdudaemon",
 "ok": true, "detail": "pdudaemon succeeded", "deleted": false}
```

`ok: false` with `method: "none"` means nothing is wired up — set
`set_pdu_alias` (if the fleet has a pdudaemon) or `set_power_command`
for that action. Powering on returns as soon as the PDU accepts;
the machine then takes a minute or two to PXE, so poll `get_machine`
until `state` reaches `login_console`.

## Reboot or recover a machine

`reboot_machine` (or `power_machine` with `action: "reboot"`) tries, in
order: the machine's own `reboot_command`, then the fleet-wide
pdudaemon fallback, then arming the soft-reboot signal so the machine
reboots itself on its next heartbeat. That last path only works if the
machine is alive enough to still be heartbeating — a wedged machine
with no power control needs hands.

`set_pending_reboot` arms or clears that signal directly, without
touching power control.

## Retire a machine

`delete_machine` removes the registry row. After that the machine is
unknown, so tftpjail default-denies it and it boots nothing. If you
also wanted it to shut down cleanly first, reboot it *before* deleting
— `reboot_machine` accepts `delete_after=true` to do both in the right
order, because a soft reboot cannot be delivered to a row that no
longer exists.

## Things that have no undo

Profile edits overwrite files on disk; `delete_machine` drops the row;
a build overwrites the artefacts for that profile + architecture. None
of these are versioned by fleetboot. Read before you write —
`get_profile` before `save_profile` — and prefer arming a soft reboot
over deleting anything you might want back.
"""


_BOOT_LIFECYCLE = """\
# The boot lifecycle

A boot reports its progress as it goes. The states are ordered, and the
server rejects out-of-order reports, so the last state you see is
genuinely the furthest a machine got.

| # | State | Emitted by | Means |
|---|-------|-----------|-------|
| 1 | `grub_running` | GRUB, over TFTP | tftpjail authorised this MAC and GRUB is executing its rendered config. |
| 2 | `kernel_loaded` | GRUB | Kernel fetched from `/boot/{token}/...`. |
| 3 | `initrd_loaded` | GRUB | Initrd fetched. |
| 4 | `booting_kernel` | GRUB | Control handed to the kernel. Anything after this is the OS's problem, not the boot chain's. |
| 5 | `network_up` | in-image reporter | The image is running and has a network. |
| 6 | `scratch_mounted` | in-image reporter | Local scratch space set up (if `scratch_mode` asks for it). |
| 7 | `nfs_mounted` | in-image reporter | Network home/shared storage mounted — only in images that use `fleetid`. |
| 8 | `login_console` | in-image reporter | The greeter is up and a human could log in. This is "fully booted". |

There is deliberately **no** `user_logged_in` state: it used to fire
from a PAM hook, but the display manager's own session tripped it too,
which made the signal misleading.

Profiles may also emit **custom states** — any other string. They are
logged and shown in the dashboard as milestones, but never advance the
highest-reached state.

## Reading a stall

- **No events at all** — the machine never got authorised. It isn't
  enrolled, no auto-enrol rule matched, or DHCP isn't pointing it at
  tftpjail.
- **Stops at `grub_running`** — GRUB is up but couldn't fetch what its
  config asked for. Usually there is no successful build for that
  profile + architecture yet.
- **Stops at `kernel_loaded` / `initrd_loaded`** — a transfer failed
  part way. Look at the size and freshness of the build artefacts.
- **Stops at `booting_kernel`** — the kernel started and the image
  never reported in: no driver for this machine's NIC, or the image
  panicked. This one usually needs a screen or a serial console
  (enrol with `serial_console=true`).
- **Stops at `network_up`** — the image is alive; something later in
  the profile's own units is failing. The reporter's diagnostics
  (systemd failed units, recent journal) ride along on the heartbeat
  and show up on the machine's dashboard page.
- **Stops at `nfs_mounted`** — identity and storage are up but the
  greeter isn't; a display-manager or graphics-driver problem.
- **Reaches `login_console`, then repeats** — normal. Every reboot
  replays the whole sequence; that is what an immutable image does.

Heartbeats continue after `login_console`, carrying hardware inventory,
the running image version, and the soft-reboot flag in the response.
A machine that stops heartbeating altogether is off, wedged, or off the
network.
"""


_ON_DEMAND_COMPUTE = """\
# Running the fleet as on-demand compute

The same registry that keeps a lab of desktops on the right image will
run that hardware as a burst compute pool. Nothing extra is installed:
a machine's whole identity is the profile it PXEs, so "give me eight
render nodes" is *power on eight machines enrolled on the render-node
profile*.

Why an immutable netboot fleet suits this:

- **Powering off costs nothing to undo.** The root filesystem is
  read-only and the overlay is tmpfs, so there is no state to drain,
  snapshot or restore. What comes back is byte-identical to what you
  switched off.
- **Provisioning is a profile edit, not a machine visit.** Change the
  image, build it, and every machine in the pool has the new toolchain
  on its next boot.
- **A worker cannot drift.** Two machines on the same profile and the
  same build version are the same machine. There is no "works on node
  3" to debug.
- **Reclaiming is instant.** `power_fleet` with `off` releases the
  hardware; nothing needs cleaning up first.

## The pattern

**1. Build a worker image.** A profile rooted at `default` with your
toolchain in `extra-packages.list` and whatever service starts the
work in `setup-chroot`. Have it emit a custom boot state when it's
genuinely ready for work — profiles can report any state string, and
it shows up in `recent_events` and on `list_machines`:

```json
{"name": "save_profile", "arguments": {
  "name": "render-node",
  "parents": ["default"],
  "extra_packages": "blender\\nnfs-common\\n"
}}
```

Then `start_build` with that profile, and poll `get_build`.

**2. Enrol the pool, with power wiring.** Each machine needs a MAC, the
profile, and a way to be switched on. Do the wiring at enrolment —
`set_pdu_alias` — because the hostname fallback only exists after a
machine has booted at least once:

```json
{"name": "enrol_machine", "arguments": {
  "mac": "aa:bb:cc:00:00:07", "profile_name": "render-node",
  "architecture": "x86_64"
}}
{"name": "set_pdu_alias", "arguments": {
  "mac": "aa:bb:cc:00:00:07", "alias": "rack1-port7"
}}
```

**3. Ask for capacity.**

```json
{"name": "power_fleet", "arguments": {
  "action": "on", "profile": "render-node", "count": 8
}}
```

The reply reports what it could actually do:

```json
{"action": "on", "profile": "render-node", "matching": 12,
 "eligible": 10, "selected": 8, "succeeded": 8, "short_by": 0,
 "results": [{"mac": "aa:bb:cc:00:00:07", "method": "pdudaemon",
              "ok": true, "detail": "pdudaemon succeeded"}]}
```

`eligible` excludes machines already online, so asking twice for eight
doesn't cycle the eight you already have. `short_by` is how many the
fleet couldn't supply — a small pool, or machines already busy.

**4. Wait for them to be usable.** `power_fleet` returns when the PDU
accepts, not when the machines are ready; a cold boot is typically a
minute or two. Poll `list_machines` and count the ones on your profile
with `online: true` and the state you're waiting for. `recent_events`
shows the boots unfolding if you want to see progress rather than just
the count.

**5. Give the hardware back.**

```json
{"name": "power_fleet", "arguments": {
  "action": "off", "profile": "render-node"
}}
```

With no `count`, every online machine on the profile goes off. There is
no drain step — make sure your work is finished or checkpointed
somewhere off the node first, because the tmpfs overlay dies with the
power.

## What this does not give you

- **No scheduler.** fleetboot switches machines on; deciding what runs
  where is your workload's job.
- **No queueing.** If the pool is short, `short_by` tells you — it does
  not wait for capacity.
- **No per-job isolation.** A machine runs one image; two jobs sharing
  a node share everything except what your profile isolates.
- **Power on is best-effort.** A machine with no PDU alias and no
  power-on command cannot be switched on, and `power_fleet` reports it
  as a failed result rather than retrying.
"""


_EXAMPLES = """\
# Talking to this endpoint

Transport is MCP Streamable HTTP in its simplest form: `POST /mcp` with
a JSON-RPC 2.0 body, one JSON body back. Notifications get `202` with
no body. Batches (an array of requests) are supported. Auth is
`Authorization: Bearer <admin secret>` — the same secret that opens the
dashboard.

## Wire up a client

```
claude mcp add --transport http fleetboot https://<host>:8080/mcp \\
    --header "Authorization: Bearer <admin secret>"
```

```json
{
  "mcpServers": {
    "fleetboot": {
      "url": "https://<host>:8080/mcp",
      "transport": "http",
      "headers": {"Authorization": "Bearer <admin secret>"}
    }
  }
}
```

## Handshake

```
curl -s -X POST https://<host>:8080/mcp \\
    -H "Authorization: Bearer <admin secret>" \\
    -H "Content-Type: application/json" \\
    -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}'
```

The result carries `instructions` — the briefing a client feeds into
its system prompt.

## List what's available

```
-d '{"jsonrpc":"2.0","id":2,"method":"tools/list"}'
-d '{"jsonrpc":"2.0","id":3,"method":"resources/list"}'
```

## Call a tool

```
-d '{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{
      "name":"list_machines","arguments":{}}}'
```

Tool results come back as one text content block. Operational tools put
JSON in it; `describe_fleetboot` puts markdown.

## Read these docs

Either as a resource:

```
-d '{"jsonrpc":"2.0","id":5,"method":"resources/read","params":{
      "uri":"fleetboot://docs/workflows"}}'
```

...or as a tool call, for clients that don't surface resources:

```
-d '{"jsonrpc":"2.0","id":6,"method":"tools/call","params":{
      "name":"describe_fleetboot","arguments":{"topic":"workflows"}}}'
```

## Errors

A malformed or unknown *method* is a JSON-RPC error object. A tool that
ran but couldn't do what you asked returns a normal result with
`isError: true` and a human-readable message in the text block — so
check `isError`, not just the HTTP status.
"""


TOPICS: tuple[DocTopic, ...] = (
    DocTopic(
        name="overview",
        title="What fleetboot is",
        summary=(
            "The system in one page: what fleetboot does, the four "
            "layers, what it deliberately isn't, and the vocabulary."
        ),
        body=_OVERVIEW,
    ),
    DocTopic(
        name="services",
        title="What fleetboot offers",
        summary=(
            "Every service on offer — MCP tools, the HTTP rails the "
            "fleet boots against, the operator API, and the companion "
            "projects."
        ),
        body=_SERVICES,
    ),
    DocTopic(
        name="profiles",
        title="Profiles: how an image is composed",
        summary=(
            "The profile contract, how inheritance merges packages, "
            "overlays and scripts, and how to author one from an agent."
        ),
        body=_PROFILES,
    ),
    DocTopic(
        name="workflows",
        title="Recipes",
        summary=(
            "Step-by-step tool sequences: enrol, roll out a new image, "
            "power on and off, diagnose, retire — and what has no undo."
        ),
        body=_WORKFLOWS,
    ),
    DocTopic(
        name="on-demand-compute",
        title="Running the fleet as on-demand compute",
        summary=(
            "Using the same hardware as a burst pool: build a worker "
            "image, enrol it with power wiring, ask for capacity, give "
            "it back — and what fleetboot deliberately doesn't do."
        ),
        body=_ON_DEMAND_COMPUTE,
    ),
    DocTopic(
        name="boot-lifecycle",
        title="The boot lifecycle",
        summary=(
            "The ordered states a boot reports, who emits each, and "
            "what a stall at each one usually means."
        ),
        body=_BOOT_LIFECYCLE,
    ),
    DocTopic(
        name="examples",
        title="Talking to this endpoint",
        summary=(
            "Raw JSON-RPC over the wire: client config, handshake, "
            "tool calls, resource reads, and how errors are shaped."
        ),
        body=_EXAMPLES,
    ),
)

DEFAULT_TOPIC = "overview"


def topic_names() -> list[str]:
    """Every documentation topic name, in reading order."""
    return [topic.name for topic in TOPICS]


def get_topic(name: str) -> DocTopic | None:
    """Look a topic up by name. Returns None if there is no such topic."""
    for topic in TOPICS:
        if topic.name == name:
            return topic
    return None


def topic_for_uri(uri: str) -> DocTopic | None:
    """Look a topic up by its `fleetboot://docs/<name>` resource URI."""
    if not uri.startswith(DOC_URI_PREFIX):
        return None
    return get_topic(uri[len(DOC_URI_PREFIX):])


def resource_descriptors() -> list[dict]:
    """The docs rendered as MCP resource descriptors for resources/list."""
    return [
        {
            "uri": topic.uri,
            "name": topic.name,
            "title": topic.title,
            "description": topic.summary,
            "mimeType": DOC_MIME_TYPE,
        }
        for topic in TOPICS
    ]


def topic_index() -> str:
    """A markdown list of every topic — appended to whatever page the
    reader landed on, so they always know what else is available."""
    lines = ["", "---", "", "**Other topics** (`describe_fleetboot`, or"
             " read `fleetboot://docs/<topic>`):", ""]
    for topic in TOPICS:
        lines.append(f"- `{topic.name}` — {topic.summary}")
    return "\n".join(lines) + "\n"
