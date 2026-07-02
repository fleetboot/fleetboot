#!/bin/sh
# Set up the fleetboot host as a Kerberos-secured NFSv4 server.
#
# Idempotent: re-running is safe; it skips work that's already in place.
#
# What it does:
#   1. Install nfs-kernel-server + nfs-common (krb5 helpers).
#   2. Get a service keytab for nfs/<this-host>@REALM from FreeIPA and
#      drop it at /etc/krb5.keytab.
#   3. Render /etc/exports from nfs/exports.template.
#   4. Render /etc/idmapd.conf from nfs/idmapd.conf.template, substituting
#      the IPA domain.
#   5. Create the export layout (/export/home and the shared skeleton).
#   6. Restart nfs-kernel-server.
#
# Prerequisites:
#   - This host is already FreeIPA-enrolled (run ipa-client-install first).
#   - You have a Kerberos admin ticket (run `kinit admin` first).
#
# Usage:
#   sudo ./scripts/setup-nfs-server.sh --realm FLEET.EXAMPLE \
#                                      --domain fleet.example

set -eu

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

REALM=""
DOMAIN=""
EXPORT_ROOT="/export"

while [ $# -gt 0 ]; do
    case "$1" in
        --realm)
            REALM="$2"
            shift 2
            ;;
        --domain)
            DOMAIN="$2"
            shift 2
            ;;
        --export-root)
            EXPORT_ROOT="$2"
            shift 2
            ;;
        *)
            echo "unknown argument: $1" >&2
            exit 2
            ;;
    esac
done

if [ -z "$REALM" ] || [ -z "$DOMAIN" ]; then
    echo "usage: $0 --realm REALM --domain DOMAIN [--export-root /export]" >&2
    exit 2
fi

if [ "$(id -u)" -ne 0 ]; then
    echo "must be run as root" >&2
    exit 1
fi

HOSTNAME_FQDN="$(hostname -f)"
PRINCIPAL="nfs/${HOSTNAME_FQDN}@${REALM}"

echo "=> installing NFS server packages"
apt-get update
apt-get install -y nfs-kernel-server nfs-common krb5-user

if [ ! -f /etc/krb5.keytab ] || ! klist -kt /etc/krb5.keytab 2>/dev/null \
        | grep -qF "$PRINCIPAL"; then
    echo "=> getting service keytab for $PRINCIPAL"
    # ipa-getkeytab requires an admin Kerberos ticket.
    if ! klist -s 2>/dev/null; then
        echo "no Kerberos ticket — run 'kinit admin' first" >&2
        exit 1
    fi
    ipa service-add "$PRINCIPAL" 2>/dev/null || true
    ipa-getkeytab -p "$PRINCIPAL" -k /etc/krb5.keytab
    chmod 0600 /etc/krb5.keytab
else
    echo "=> keytab already has $PRINCIPAL"
fi

echo "=> writing /etc/exports"
install -m 0644 "$REPO_ROOT/nfs/exports.template" /etc/exports

echo "=> rendering /etc/idmapd.conf"
sed -e "s|__IPA_DOMAIN__|$DOMAIN|" \
    "$REPO_ROOT/nfs/idmapd.conf.template" > /etc/idmapd.conf
chmod 0644 /etc/idmapd.conf

echo "=> creating export layout under $EXPORT_ROOT"
install -d -m 0755 "$EXPORT_ROOT"
install -d -m 0755 "$EXPORT_ROOT/home"
install -d -m 0755 "$EXPORT_ROOT/shared"
# The shared skeleton — each subdir gets group ownership wired up later
# by the admin once the FreeIPA groups exist.
install -d -m 1777 "$EXPORT_ROOT/shared/all"

echo "=> enabling and restarting NFS services"
systemctl enable --now nfs-kernel-server
systemctl restart nfs-kernel-server
# nfs-idmapd is socket-activated on modern Debian; restart to pick up
# /etc/idmapd.conf changes.
systemctl restart nfs-idmapd || true

exportfs -ra
echo
echo "NFS server is up. exportfs reports:"
exportfs -v
echo
echo "Next steps:"
echo "  - Wire the export layout per nfs/shared-skeleton.md."
echo "  - Set IPA_NFS_SERVER=$HOSTNAME_FQDN in fleetboot's identity.conf."
