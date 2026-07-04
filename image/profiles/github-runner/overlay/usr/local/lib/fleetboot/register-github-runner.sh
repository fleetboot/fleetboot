#!/bin/sh
# Configure + start a GitHub Actions self-hosted runner, ephemeral,
# one job per boot. Called by fleetboot-github-runner.service.
#
# All GitHub-specific settings come from /etc/fleetboot-runner.conf,
# which the admin provides via image/custom/overlay/ or their own
# overlay. Fleetboot itself knows nothing about GitHub.
#
# Everything this script prints — including the actions/runner's
# `./run.sh` output — is teed to /dev/tty1, and to /dev/ttyS0 when
# the machine was booted with serial console enabled. That way a
# plugged-in monitor or serial cable becomes the runner's
# live-status display; there's no login prompt to compete with it
# (getty units are masked by the profile's setup-chroot).

set -eu

# Build the list of console devices we mirror output to.
consoles="/dev/tty1"
if grep -qE 'console=ttyS[0-9]' /proc/cmdline 2>/dev/null; then
    consoles="$consoles /dev/ttyS0"
fi

# Everything from here down runs in a subshell whose stdout+stderr
# are piped through `tee` to the console(s). `tee`'s own stdout
# still flows to systemd's journal so `journalctl -u
# fleetboot-github-runner.service` also has the full log.
{

CONF=/etc/fleetboot-runner.conf

if [ ! -r "$CONF" ]; then
    echo "register-github-runner: $CONF not present; nothing to do" >&2
    echo "  (see image/profiles/github-runner/README.md for the format)" >&2
    exit 0
fi

# shellcheck disable=SC1090
. "$CONF"

if [ -z "${RUNNER_URL:-}" ]; then
    echo "register-github-runner: RUNNER_URL not set in $CONF" >&2
    exit 1
fi

# Obtain a fresh registration token via one of two admin-chosen
# mechanisms. REG_TOKEN_URL wins if both are set (fresh-per-boot).
if [ -n "${REG_TOKEN_URL:-}" ]; then
    RESPONSE="$(mktemp)"
    trap 'rm -f "$RESPONSE"' EXIT
    if ! curl --fail -sS "$REG_TOKEN_URL" -o "$RESPONSE"; then
        echo "register-github-runner: REG_TOKEN_URL fetch failed" >&2
        exit 1
    fi
    REG_TOKEN="$(jq -r .token "$RESPONSE")"
elif [ -z "${REG_TOKEN:-}" ]; then
    echo "register-github-runner: set REG_TOKEN_URL or REG_TOKEN in $CONF" >&2
    exit 1
fi

if [ -z "$REG_TOKEN" ] || [ "$REG_TOKEN" = "null" ]; then
    echo "register-github-runner: no registration token in response" >&2
    exit 1
fi

# Runner name: the machine's hostname is a good default (fleetboot
# already ensures every machine has a unique deterministic hostname).
RUNNER_NAME="$(hostname)"

cd /opt/actions-runner

# config.sh writes state into /opt/actions-runner. The tmpfs overlay
# means all of it vanishes at power-off — exactly what --ephemeral
# expects.
./config.sh \
    --unattended \
    --ephemeral \
    --url "$RUNNER_URL" \
    --token "$REG_TOKEN" \
    --name "$RUNNER_NAME" \
    --labels "${RUNNER_LABELS:-self-hosted,linux,x64,fleetboot}" \
    --replace

exec ./run.sh

} 2>&1 | tee $consoles
