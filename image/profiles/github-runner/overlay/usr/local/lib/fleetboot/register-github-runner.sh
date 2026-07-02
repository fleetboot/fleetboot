#!/bin/sh
# Configure + start a GitHub Actions self-hosted runner, ephemeral,
# one job per boot. Called by fleetboot-github-runner.service.
#
# All GitHub-specific settings come from /etc/fleetboot-runner.conf,
# which the admin provides via image/custom/overlay/ or their own
# overlay. Fleetboot itself knows nothing about GitHub.

set -eu

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
