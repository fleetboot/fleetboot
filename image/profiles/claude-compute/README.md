# `claude-compute` profile

A headless compute worker built for **AI agents that drive fleetboot
over MCP**. The agent powers a machine on when it has work, SSHes in to
run it, and powers it back off when it is done — the fleet as an
on-demand burst pool rather than a set of always-on servers.

It suits an immutable netboot fleet perfectly: powering a worker off
costs nothing to undo (read-only squashfs root + tmpfs overlay means
there is no state to drain), and a worker that comes back is
byte-identical to the one that was switched off. See the MCP
`on-demand-compute` topic (`describe_fleetboot(topic="on-demand-compute")`)
for the full pattern this profile is the worker half of.

## What ships

- **SSH** (from the `ssh-debug` parent): hardened sshd, pubkeys only,
  root login by key. The `authorized_keys` file is **not** baked into
  the image — it is fetched at boot from fleetboot, so the squashfs
  never contains anyone's key.
- **A general-purpose compute toolchain** (`extra-packages.list`):
  `build-essential`, Python 3 with `venv`/`pip`/`-dev`, `git`, `curl`,
  `wget`, `rsync`, `jq`, `podman` (rootless, daemonless containers),
  and `tmux`/`htop`. Anything job-specific the agent installs at run
  time into the tmpfs overlay, so the image stays small and each boot
  is a clean slate.
- **`fleetboot-compute-ready.service`**: reports a `compute_ready` boot
  state once the network is up and sshd is accepting connections. An
  agent that called `power_fleet` polls for this to know the worker is
  actually usable, not merely pinging.

## Giving the agent SSH access

This profile ships a **deliberately open login** so an agent needs no
key to get in:

- user **`fleetboot`**, password **`fleetboot`**, with **passwordless
  sudo**;
- SSH **password authentication is on** (this profile overrides the
  `ssh-debug` parent's password-off default).

So once a worker is up an agent just runs `ssh fleetboot@<worker>`
(password `fleetboot`) and can `sudo apt-get install …` whatever the job
needs. Nothing to provision.

> ⚠️ **This is insecure by design and scoped to this worker profile.**
> The credentials are baked into the image and identical on every
> machine that boots it. It is acceptable only because the worker is
> transient (tmpfs overlay, powered off after the job) on a trusted
> admin network. **Never put `claude-compute` on the permanent / school
> fleet.**

Key-based root login still works too, inherited from `ssh-debug`: if you
set the deployment-wide `FLEETBOOT_AUTHORIZED_KEYS` file, its keys are
fetched at boot and grant `ssh root@<worker>`. You don't need it for the
password login above.

## Scratch disk

The worker's root is tmpfs and dies with the power, which is usually
what you want. If a job needs local scratch bigger than RAM, enrol the
machine with `scratch_mode="volatile"` (a fresh ext4 at `/var/scratch`
every boot) or `"persistent"` (kept across reboots). `"off"`, the
default here if you don't ask otherwise, leaves the local disk
untouched. Persistent state that must outlive the worker belongs on an
NFS/`/home` mount, not on the local disk.

## The agent's workflow

1. `enrol_machine` each worker on `claude-compute`, then `set_pdu_alias`
   (or `set_power_command`) so it can be switched on.
2. `power_fleet` with `action="on"`, `profile="claude-compute"`, and a
   `count` when there's work.
3. Poll `list_machines` for the workers on this profile that are
   `online` with state `compute_ready`, then SSH in and run the job.
4. **Power the pool back off the moment the work is done** — `power_fleet`
   with `action="off"`, `profile="claude-compute"`. An idle worker left
   on burns real power and holds hardware others could use. Checkpoint
   anything you need off the node first: the tmpfs overlay dies with the
   power.

## Build

```sh
make image PROFILE=claude-compute
```

Produces `build/fleetboot-claude-compute-amd64.squashfs`.

## Design notes

- **Headless.** No desktop parent — a compute worker doesn't need a
  greeter. Add a desktop profile to `parent` only if you want a local
  display for debugging.
- **Root over SSH.** `ssh-debug` delivers keys to `/root/.ssh`, so the
  agent works as root; `podman` runs containers normally from there. Add
  a deployment overlay if you'd rather the agent land as a non-root user.
- **One image, many workers.** Every machine on this profile at the same
  build version is the same machine. There is no "works on node 3" — a
  worker cannot drift.
