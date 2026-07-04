#!/bin/sh
# Configure + start a GitHub Actions self-hosted runner as a
# persistent worker (registered once per boot, then serves job
# after job). Called by fleetboot-github-runner.service.
#
# All GitHub-specific settings come from /etc/fleetboot-runner.conf,
# which the admin provides via image/custom/overlay/ or their own
# overlay. Fleetboot itself knows nothing about GitHub.
#
# The whole script mirrors its stdout+stderr to /dev/tty1, and to
# /dev/ttyS0 when the machine was booted with serial console
# enabled. Admins with a monitor or serial cable watch job progress
# live; there's no login prompt to compete with it (getty units are
# masked by the profile's setup-chroot).

set -eu

# Build the list of console devices we mirror output to.
consoles="/dev/tty1"
if grep -qE 'console=ttyS[0-9]' /proc/cmdline 2>/dev/null; then
    consoles="$consoles /dev/ttyS0"
fi

# Wrap the entire script's output through tee to the console(s).
# The service runs as root so tty1 / ttyS0 permissions are trivial.
# tee's own stdout still flows to systemd, so
# `journalctl -u fleetboot-github-runner.service` also has the log.
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

# Ownership: the runner binaries and state live under
# /opt/actions-runner, owned by `runner`. config.sh will write into
# .credentials there. We run config + run as that non-root user via
# `runuser` — everything after this point drops privileges.
chown -R runner:runner /opt/actions-runner

runuser -u runner -- /opt/actions-runner/config.sh \
    --unattended \
    --url "$RUNNER_URL" \
    --token "$REG_TOKEN" \
    --name "$RUNNER_NAME" \
    --labels "${RUNNER_LABELS:-self-hosted,linux,x64,fleetboot}" \
    --replace

# Tell fleetboot the runner is up. This is a *custom* state — the
# server's BootState enum doesn't know about it, so it's recorded
# purely as a boot event on the machine's timeline (no ranking).
# Admin sees "runner_started" appear in the events stream just
# before the runner starts polling for jobs.
/usr/bin/python3 -m fleetboot.reporter.report runner_started \
    || echo "register-github-runner: state report failed (non-fatal)" >&2

echo "register-github-runner: config complete, starting run.sh"
exec runuser -u runner -- /opt/actions-runner/run.sh

} 2>&1 | tee $consoles
