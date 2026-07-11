#!/bin/sh
# NFS server entrypoint.
#
# 1. Render /etc/exports and /etc/idmapd.conf from templates.
# 2. Create the export skeleton if it's missing.
# 3. Bring up rpcbind, rpc.gssd, rpc.idmapd, then nfsd.
#
# Requires from the environment:
#   IPA_DOMAIN — realm's DNS domain, e.g. school.example.com
#                (used as the NFSv4 idmap Domain — every host in the
#                deployment must agree).
#
# Optional:
#   NFS_EXPORT_ROOT  — where /export is inside the container, default /export.
#   NFSD_THREADS     — kernel nfsd thread count, default 8.
set -eu

: "${IPA_DOMAIN:?IPA_DOMAIN must be set — the NFSv4 idmap Domain}"
NFS_EXPORT_ROOT="${NFS_EXPORT_ROOT:-/export}"
NFSD_THREADS="${NFSD_THREADS:-8}"

echo "[nfs-entrypoint] rendering /etc/exports"
cp /templates/exports.template /etc/exports

echo "[nfs-entrypoint] rendering /etc/idmapd.conf (Domain=${IPA_DOMAIN})"
sed "s|__IPA_DOMAIN__|${IPA_DOMAIN}|g" \
    /templates/idmapd.conf.template > /etc/idmapd.conf

# Skeleton layout — see nfs/shared-skeleton.md. mkdir -p is idempotent
# so re-runs after the first `up` don't stomp on ownership/permission
# tweaks the admin made in-place.
echo "[nfs-entrypoint] ensuring export skeleton under ${NFS_EXPORT_ROOT}"
mkdir -p "${NFS_EXPORT_ROOT}/home"
mkdir -p "${NFS_EXPORT_ROOT}/shared/all"
mkdir -p "${NFS_EXPORT_ROOT}/shared/teachers"
mkdir -p "${NFS_EXPORT_ROOT}/shared/headmaster"
mkdir -p "${NFS_EXPORT_ROOT}/shared/students"
mkdir -p "${NFS_EXPORT_ROOT}/shared/coursework"

if [ ! -f /etc/krb5.keytab ]; then
    echo "[nfs-entrypoint] WARNING: /etc/krb5.keytab missing" >&2
    echo "[nfs-entrypoint]   krb5p exports will refuse to authenticate" >&2
    echo "[nfs-entrypoint]   see scripts/setup-nfs-server.sh for how to" >&2
    echo "[nfs-entrypoint]   fetch nfs/<host>@REALM from FreeIPA and" >&2
    echo "[nfs-entrypoint]   drop it on the host at ../fleetboot/tls-keytabs/nfs.keytab" >&2
fi

# The kernel nfsd module must be loaded (containers can't load it
# themselves — depends on host's /lib/modules). Try modprobe and warn
# on failure; nfsd usually auto-loads when rpc.nfsd runs, but this
# gives us a clearer error surface.
if [ -e /proc/sys/net/rpc ]; then
    :
else
    echo "[nfs-entrypoint] loading nfsd module"
    modprobe nfsd 2>/dev/null || echo "[nfs-entrypoint]   modprobe failed; " \
        "kernel-side setup may be missing on the host" >&2
fi

# rpcbind is the portmapper — everything below registers with it.
echo "[nfs-entrypoint] starting rpcbind"
rpcbind -w

# rpc.gssd handles the Kerberos side for clients ATTACHING to the
# server; on the server side rpc.svcgssd (or the kernel-side gss)
# handles inbound. Debian 13 ships kernel-side rpcsec_gss so we
# just need rpc.idmapd for NFSv4 name<->uid mapping.
echo "[nfs-entrypoint] starting rpc.idmapd"
rpc.idmapd -f &
IDMAPD_PID=$!

# exportfs re-reads /etc/exports; -a exports everything, -r syncs
# the in-kernel table.
echo "[nfs-entrypoint] exporting filesystems"
exportfs -ra

echo "[nfs-entrypoint] starting rpc.mountd"
rpc.mountd

echo "[nfs-entrypoint] starting nfsd (${NFSD_THREADS} threads)"
rpc.nfsd "${NFSD_THREADS}"

echo "[nfs-entrypoint] NFS server up. Tail /proc/mounts to inspect."

# Wait on idmapd — if it exits, our name-mapping is broken and the
# container should restart.
wait "${IDMAPD_PID}"
