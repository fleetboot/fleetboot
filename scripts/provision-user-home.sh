#!/bin/sh
# Provision an NFS home directory for a Kerberos-realm user.
#
# The NFS server (host or container) sees the user's UID/GID via SSSD
# querying FreeIPA. This script:
#   1. Resolves the user via `getent passwd`.
#   2. Creates /export/home/<user> if missing.
#   3. Chowns it to the SSSD-resolved UID:primary-GID.
#   4. Sets mode 0700 so the user is the only one who can enter it.
#
# Idempotent: re-running is a no-op for existing homes with correct
# ownership. Race-safe against concurrent first-logins as long as the
# `mkdir -p` semantics on the underlying filesystem are POSIX.
#
# Usage:
#   sudo ./scripts/provision-user-home.sh <username> [<username> ...]
#
# Or from cron / a systemd path unit:
#   getent passwd | awk -F: '$3 >= 1000 && $3 < 65534 {print $1}' \
#     | xargs -r ./scripts/provision-user-home.sh
#
# Environment:
#   EXPORT_HOME_ROOT — where /export/home lives, default /export/home.
#                      Point at /srv/fleetboot/nfs/export/home when
#                      running from the compose host outside the
#                      container.

set -eu

EXPORT_HOME_ROOT="${EXPORT_HOME_ROOT:-/export/home}"

if [ "$#" -eq 0 ]; then
    echo "usage: $0 <username> [<username> ...]" >&2
    exit 2
fi

if [ "$(id -u)" -ne 0 ]; then
    echo "error: must run as root (sudo) — mkdir + chown for other users" >&2
    exit 1
fi

if [ ! -d "$EXPORT_HOME_ROOT" ]; then
    echo "error: $EXPORT_HOME_ROOT does not exist" >&2
    echo "  create it first (this script won't guess the correct owner)" >&2
    exit 1
fi

provision_one() {
    user="$1"
    # `getent passwd` consults nsswitch — SSSD provides IPA users when
    # the host is FreeIPA-enrolled. Local /etc/passwd users are also
    # accepted (e.g. for testing without IPA).
    entry="$(getent passwd "$user" || true)"
    if [ -z "$entry" ]; then
        echo "[$user] not found in nsswitch (SSSD?) — skipping" >&2
        return 1
    fi
    uid="$(echo "$entry" | cut -d: -f3)"
    gid="$(echo "$entry" | cut -d: -f4)"

    home="$EXPORT_HOME_ROOT/$user"

    if [ ! -d "$home" ]; then
        mkdir -p "$home"
        echo "[$user] created $home"
    fi

    # Always reassert ownership + mode. A previous run that mkdir'd
    # under a different UID (e.g. a repurposed username) would leave
    # the wrong owner in place; this makes the script self-correcting.
    chown "$uid:$gid" "$home"
    chmod 0700 "$home"
    echo "[$user] $home owner=$uid:$gid mode=0700"
}

exit_code=0
for user in "$@"; do
    provision_one "$user" || exit_code=1
done
exit "$exit_code"
