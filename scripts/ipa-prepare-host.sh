#!/bin/sh
# Mint a per-MAC FreeIPA enrolment keytab and drop it where fleetboot's
# /enrol endpoint can deliver it on first boot.
#
# What it does:
#   1. Derive a host FQDN from the MAC (configurable).
#   2. `ipa host-add` inside the freeipa-server container.
#   3. `ipa-getkeytab` for that host into a file we copy out.
#   4. Place the keytab at <keytabs_dir>/<mac>.keytab (mode 0600).
#
# Required environment:
#   IPA_ADMIN_PASS    — IPA admin password
#
# Optional environment:
#   IPA_CONTAINER     — container name (default: freeipa-server)
#   IPA_DOMAIN        — FreeIPA domain (default: fleetboot.lan)
#   IPA_KEYTABS_DIR   — where fleetboot serves keytabs from
#                       (default: /var/lib/fleetboot/keytabs)
#   IPA_HOST_PREFIX   — prefix for derived hostnames
#                       (default: fleetboot)
#
# Usage:
#   sudo IPA_ADMIN_PASS=... ./scripts/ipa-prepare-host.sh aa:bb:cc:dd:ee:ff

set -eu

CONTAINER="${IPA_CONTAINER:-freeipa-server}"
DOMAIN="${IPA_DOMAIN:-fleetboot.lan}"
KEYTABS_DIR="${IPA_KEYTABS_DIR:-/var/lib/fleetboot/keytabs}"
HOST_PREFIX="${IPA_HOST_PREFIX:-fleetboot}"

if [ -z "${1:-}" ]; then
    echo "usage: $0 <mac>" >&2
    exit 2
fi
RAW_MAC="$1"
MAC=$(echo "$RAW_MAC" | tr 'A-Z' 'a-z' | tr -- '-.' ':')

# Sanity-check the MAC shape: six lowercase hex pairs separated by colons.
case "$MAC" in
    [0-9a-f][0-9a-f]:[0-9a-f][0-9a-f]:[0-9a-f][0-9a-f]:[0-9a-f][0-9a-f]:[0-9a-f][0-9a-f]:[0-9a-f][0-9a-f])
        ;;
    *)
        echo "error: invalid MAC '$RAW_MAC'" >&2
        exit 2
        ;;
esac

if [ -z "${IPA_ADMIN_PASS:-}" ]; then
    echo "error: IPA_ADMIN_PASS must be set" >&2
    exit 2
fi

if ! docker inspect "$CONTAINER" >/dev/null 2>&1; then
    echo "error: container '$CONTAINER' not found" >&2
    exit 1
fi

# fleetboot's normalisation puts keytab files at <mac>.keytab — same
# colon-form we just normalised to.
KEYTAB_FILE="$KEYTABS_DIR/$MAC.keytab"
mkdir -p "$KEYTABS_DIR"
chmod 0700 "$KEYTABS_DIR"

# Derive a hostname from the MAC. We strip the colons so it's a valid
# DNS label; the IPA domain is appended.
HOST_LABEL=$(echo "$MAC" | tr -d ':')
HOST_FQDN="${HOST_PREFIX}-${HOST_LABEL}.${DOMAIN}"

echo "preparing IPA enrolment for:"
echo "   MAC:     $MAC"
echo "   FQDN:    $HOST_FQDN"
echo "   keytab:  $KEYTAB_FILE"

# Wrapper to run a command in the container with an admin kinit.
ipa_in_container() {
    docker exec -i "$CONTAINER" sh -c "
        echo \"$IPA_ADMIN_PASS\" | kinit admin >/dev/null 2>&1 || exit 1
        $*
    "
}

# 1. Create the host entry. `--force` skips the DNS resolution check so
#    we don't need IPA to know about every host in advance.
if ipa_in_container "ipa host-show $HOST_FQDN" >/dev/null 2>&1; then
    echo "host $HOST_FQDN already in IPA"
else
    echo "adding host $HOST_FQDN to IPA"
    ipa_in_container "ipa host-add $HOST_FQDN --force"
fi

# 2. Get a keytab. We need to do this inside the container, then copy it
#    out. ipa-getkeytab without -P generates a random one-time-use OTP.
CONTAINER_KEYTAB="/tmp/$(basename "$KEYTAB_FILE")"
docker exec "$CONTAINER" sh -c "
    echo \"$IPA_ADMIN_PASS\" | kinit admin >/dev/null 2>&1 || exit 1
    rm -f $CONTAINER_KEYTAB
    ipa-getkeytab -p host/$HOST_FQDN -k $CONTAINER_KEYTAB
    chmod 0600 $CONTAINER_KEYTAB
"

# 3. Copy out of the container and drop into the keytabs dir.
docker cp "$CONTAINER:$CONTAINER_KEYTAB" "$KEYTAB_FILE"
chmod 0600 "$KEYTAB_FILE"
docker exec "$CONTAINER" rm -f "$CONTAINER_KEYTAB"

echo
echo "=> done. fleetboot will deliver this keytab on first boot via"
echo "   GET /enrol/<token>/keytab when MAC $MAC powers on."
