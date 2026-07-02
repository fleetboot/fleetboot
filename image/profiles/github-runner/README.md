# `github-runner` profile

Example profile: turn a fleet machine into a GitHub Actions
self-hosted runner that registers itself on every boot, runs one
job, and exits (ephemeral). Perfect fit for a netboot fleet — the
image is immutable, so each boot is a fresh, uncontaminated CI
worker.

Fleetboot itself knows nothing about GitHub. This profile is a
worked example that reads its configuration from a file the
**admin** puts on the image via `image/custom/overlay/`.

## What ships

- The `actions/runner` binary (downloaded from GitHub's public
  releases in `setup-chroot`, version pinned in that script).
- A local `runner` user (no password, no shell login).
- `fleetboot-github-runner.service` — systemd unit that runs the
  registration + run script on boot.
- `/usr/local/lib/fleetboot/register-github-runner.sh` — reads
  `/etc/fleetboot-runner.conf`, obtains a registration token, and
  starts the runner.

## Admin responsibility

Provide `/etc/fleetboot-runner.conf` via your own overlay (e.g.
under `image/custom/overlay/`). It must be a shell fragment that
exports:

```sh
# The URL of the org, repo, or enterprise you want the runner in.
RUNNER_URL="https://github.com/your-org"

# Comma-separated labels the runner advertises. Optional.
RUNNER_LABELS="self-hosted,linux,x64,fleetboot"

# EXACTLY ONE of the following two: how the script obtains a
# short-lived registration token.
#
#   REG_TOKEN_URL — hit this URL (auth up to you) to get a JSON
#     response {"token": "..."} with a fresh registration token.
#     Recommended: run your own tiny service alongside fleetboot
#     that calls GitHub's `POST /orgs/<org>/actions/runners
#     /registration-token` API using an org PAT.
#
#   REG_TOKEN — a pre-minted registration token, straight from
#     GitHub. Short-lived (~1 hour) so you'll need to rebuild the
#     image often. Fine for one-off tests.
#
# REG_TOKEN_URL="https://your-runner-broker.internal/mint"
# REG_TOKEN="AAAA...ZZZZ"
```

If the config file is missing, the systemd unit logs and exits
0 — the machine boots normally as an idle host.

## Build

```sh
make image PROFILE=github-runner
```

Produces `build/fleetboot-github-runner-amd64.squashfs`.

## Design notes

- **Ephemeral runners only.** A persistent runner would cache
  credentials on disk that don't survive the tmpfs overlay
  anyway.
- **One job per boot.** After the runner exits the systemd unit
  stops. Chain in `pending_reboot`-style rearm or just let the
  machine sit idle until the next scheduled cycle.
- **Headless.** No desktop parent. If you want a display for
  local debugging, add `ssh-debug` or a desktop profile to this
  profile's `parent` file.
