#!/bin/sh
# Fetch root's SSH authorized_keys from fleetboot at boot time.
#
# Same pattern as fleetboot-keytab-fetch: read the per-boot token from
# /proc/cmdline, GET /enrol/<token>/authorized_keys, write to
# /root/.ssh/authorized_keys with safe perms. Failure is silent and
# non-fatal — without keys, sshd refuses logins (set up by
# ssh-debug's setup-chroot) but the rest of the boot continues.
#
# Why fetch at boot instead of baking into the image: the keys are
# admin pubkeys, not per-machine secrets, but the squashfs lives on
# the network where any unenrolled client can in principle download
# it. Boot-token-gated delivery means the keys only reach a machine
# that's currently going through PXE — same threat model as the
# FreeIPA enrolment keytab.

set -eu

KEYS_PATH=/root/.ssh/authorized_keys

# Already populated by some previous run / overlay? Don't overwrite.
if [ -s "$KEYS_PATH" ]; then
    echo "fleetboot-fetch-authorized-keys: $KEYS_PATH already non-empty; skipping"
    exit 0
fi

# Read fleetboot context from cmdline via the reporter's parser. Lives
# at /usr/lib/python3/dist-packages/fleetboot in the image.
exec /usr/bin/python3 - <<'PY'
import sys
from pathlib import Path

import httpx

from fleetboot.reporter.cmdline import (
    MissingReporterSettingsError, read_settings,
)


KEYS_PATH = Path("/root/.ssh/authorized_keys")
TIMEOUT = 10

try:
    settings = read_settings()
except MissingReporterSettingsError as err:
    print(f"fleetboot-fetch-authorized-keys: no settings: {err}", file=sys.stderr)
    sys.exit(0)

url = (
    settings.server_url.rstrip("/")
    + f"/enrol/{settings.boot_token}/authorized_keys"
)
try:
    with httpx.Client(timeout=TIMEOUT) as client:
        response = client.get(url)
except httpx.HTTPError as err:
    print(f"fleetboot-fetch-authorized-keys: transport: {err}", file=sys.stderr)
    sys.exit(0)

if response.status_code == 404:
    print("fleetboot-fetch-authorized-keys: no keys provisioned; skipping")
    sys.exit(0)
if response.status_code >= 400:
    print(
        f"fleetboot-fetch-authorized-keys: server returned "
        f"{response.status_code}; skipping",
        file=sys.stderr,
    )
    sys.exit(0)

KEYS_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
KEYS_PATH.write_bytes(response.content)
KEYS_PATH.chmod(0o600)
print(f"fleetboot-fetch-authorized-keys: wrote {len(response.content)} bytes")
PY
