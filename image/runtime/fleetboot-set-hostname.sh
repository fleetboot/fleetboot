#!/bin/sh
# Adopt the DHCP-supplied hostname from initramfs' ipconfig output.
#
# When the kernel cmdline contains `ip=dhcp` (it does for every fleetboot
# boot), the klibc `ipconfig` tool in the initramfs brings up networking
# and writes per-interface state to /run/net-<iface>.conf. That file
# includes HOSTNAME=<value> if the DHCP server returned option 12.
#
# We can't rely on NetworkManager or dhclient firing their respective
# hook scripts later — NM defaults to its internal DHCP backend and may
# decide the interface is already configured (so no DHCP4_HOST_NAME
# event), and live-boot doesn't use dhclient at all in the initramfs.
# Reading /run/net-*.conf is the universal source of truth.

set -eu

found=""
for conf in /run/net-*.conf; do
    [ -f "$conf" ] || continue
    # Lines look like:    HOSTNAME='optiplex780core2duo'
    # The quotes are sometimes single, sometimes absent — strip both.
    hn=$(awk -F= '/^HOSTNAME=/{
        gsub(/^["\047]|["\047]$/, "", $2)
        print $2
        exit
    }' "$conf")
    if [ -n "$hn" ]; then
        found="$hn"
        break
    fi
done

if [ -z "$found" ]; then
    echo "fleetboot-set-hostname: no HOSTNAME in /run/net-*.conf; leaving as-is" >&2
    exit 0
fi

echo "fleetboot-set-hostname: setting hostname to $found"
hostnamectl set-hostname --static "$found" 2>/dev/null || hostname "$found"
