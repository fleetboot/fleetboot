#!/bin/sh
# Run the upstream FreeIPA server as a Docker container.
#
# FreeIPA server isn't packaged for Debian (only the client is), so the
# Linux-typical path is to run the official Fedora-based container image
# via Docker. This script:
#
#   1. Pulls the freeipa/freeipa-server image.
#   2. Creates a persistent data dir (default /var/lib/ipa-data, mode 0700).
#   3. Runs the container with the required capabilities and port mappings.
#   4. Inside the container, runs ipa-server-install unattended with the
#      passwords from the environment.
#
# Required environment:
#   IPA_ADMIN_PASS  — initial admin password (min 8 chars)
#   IPA_DM_PASS     — initial Directory Manager password (min 8 chars)
#
# Optional environment (with sensible defaults shown):
#   IPA_REALM=FLEETBOOT.LAN
#   IPA_DOMAIN=fleetboot.lan
#   IPA_SERVER_FQDN=ipa.fleetboot.lan
#   IPA_DATA_DIR=/var/lib/ipa-data
#   IPA_IMAGE=freeipa/freeipa-server:fedora-rawhide
#
# What this MODIFIES on the host:
#   - opens TCP/UDP 53 (DNS), TCP 80/443 (web), TCP/UDP 88 (krb5),
#     TCP/UDP 464 (kpasswd), TCP 389/636 (LDAP/LDAPS), TCP 749 (kadmin).
#   - creates IPA_DATA_DIR (mode 0700) for the container's state.
#   - the host must NOT already run a daemon bound to those ports —
#     systemd-resolved on 53, BIND on 53, slapd on 389, etc. will conflict.
#
# Initial install takes ~5–10 minutes. Watch progress with:
#   docker logs -f freeipa-server

set -eu

IPA_REALM="${IPA_REALM:-FLEETBOOT.LAN}"
IPA_DOMAIN="${IPA_DOMAIN:-fleetboot.lan}"
IPA_SERVER_FQDN="${IPA_SERVER_FQDN:-ipa.${IPA_DOMAIN}}"
IPA_DATA_DIR="${IPA_DATA_DIR:-/var/lib/ipa-data}"
IPA_IMAGE="${IPA_IMAGE:-freeipa/freeipa-server:fedora-rawhide}"

if [ -z "${IPA_ADMIN_PASS:-}" ] || [ -z "${IPA_DM_PASS:-}" ]; then
    echo "error: IPA_ADMIN_PASS and IPA_DM_PASS must be set" >&2
    echo "       (both at least 8 chars; see the top of this script)" >&2
    exit 2
fi

if ! command -v docker >/dev/null 2>&1; then
    echo "error: docker not found" >&2
    exit 1
fi

if [ "$(id -u)" -ne 0 ]; then
    echo "error: must run as root (sudo)" >&2
    exit 1
fi

# Check none of the IPA-required ports are already bound. We rely on `ss`;
# fail loudly so the admin can stop the conflicting service first.
echo "=> pre-flight: checking required ports are free"
required_tcp="53 80 88 389 443 464 636 749"
required_udp="53 88 464"
for port in $required_tcp; do
    if ss -tlnp 2>/dev/null | grep -qE "[:.]${port}[[:space:]]"; then
        echo "error: TCP port $port already in use; stop the listener first" >&2
        ss -tlnp 2>/dev/null | grep -E "[:.]${port}[[:space:]]" >&2 || true
        exit 1
    fi
done
for port in $required_udp; do
    if ss -ulnp 2>/dev/null | grep -qE "[:.]${port}[[:space:]]"; then
        echo "error: UDP port $port already in use; stop the listener first" >&2
        ss -ulnp 2>/dev/null | grep -E "[:.]${port}[[:space:]]" >&2 || true
        exit 1
    fi
done

# Prepare persistent data dir.
mkdir -p "$IPA_DATA_DIR"
chmod 0700 "$IPA_DATA_DIR"

# Existing container? bail loudly — re-running install on a non-empty data
# dir is what `--first-boot` is for, but it's easier to wrap that in a
# separate "restart-ipa-server.sh" once the install has succeeded once.
if docker inspect freeipa-server >/dev/null 2>&1; then
    echo "error: container 'freeipa-server' already exists" >&2
    echo "       (remove with 'docker rm -f freeipa-server' or use" \
         "'docker start freeipa-server' to resume)" >&2
    exit 1
fi

echo "=> pulling $IPA_IMAGE"
docker pull "$IPA_IMAGE"

echo "=> launching IPA server (this takes 5–10 minutes)"
docker run -d \
    --name freeipa-server \
    --hostname "$IPA_SERVER_FQDN" \
    --read-only \
    --sysctl net.ipv6.conf.all.disable_ipv6=0 \
    --security-opt seccomp=unconfined \
    --security-opt apparmor=unconfined \
    --security-opt label=disable \
    --tmpfs /run --tmpfs /tmp \
    -v "$IPA_DATA_DIR:/data:Z" \
    -p 53:53/udp -p 53:53 \
    -p 80:80 -p 443:443 \
    -p 88:88 -p 88:88/udp \
    -p 749:749 \
    -p 464:464 -p 464:464/udp \
    -p 389:389 -p 636:636 \
    --restart unless-stopped \
    "$IPA_IMAGE" \
    -U \
    -r "$IPA_REALM" \
    -n "$IPA_DOMAIN" \
    -p "$IPA_DM_PASS" \
    -a "$IPA_ADMIN_PASS" \
    --no-ntp \
    --setup-dns --no-forwarders --no-reverse \
    --auto-reverse \
    --no-host-dns

cat <<EOF

=> IPA server container is starting up.
=> Watch progress:    docker logs -f freeipa-server
=> When done, you should see "FreeIPA server configured."

=> Realm:    $IPA_REALM
=> Domain:   $IPA_DOMAIN
=> Server:   $IPA_SERVER_FQDN

Next steps once the install completes:
  1. Add a DNS / /etc/hosts entry pointing $IPA_SERVER_FQDN at this
     host's IP, so dev VMs can find it.
  2. Run scripts/ipa-add-test-users.sh to create dev accounts.
  3. For each fleetboot machine, run scripts/ipa-prepare-host.sh
     <mac> to mint a one-time enrolment keytab into keytabs_dir.
EOF
