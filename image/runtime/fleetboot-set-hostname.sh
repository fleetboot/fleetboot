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
# Two-step: overwrite /etc/hostname so any later service that reads it
# (NetworkManager, DHCP clients, login banners) sees the right value,
# AND call the `hostname` syscall directly so the live kernel name
# updates without a reboot. Going via the d-bus hostname service is
# unreliable here — this script runs in early boot, before the socket
# activation has wired up that service, so the d-bus call silently
# fails and NetworkManager later clobbers the transient back from
# /etc/hostname.
echo "$found" > /etc/hostname
hostname "$found"
