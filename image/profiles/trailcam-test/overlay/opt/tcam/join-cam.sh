#!/bin/sh
# Join a TrailCam Go soft-AP on the USB WiFi dongle ONLY, leaving the wired
# netboot interface (and the fleet control link) untouched.
#
#   join-cam.sh SSID PASSWORD [IFACE]
#
# If IFACE is omitted the first wireless interface from `iw dev` is used.
set -eu

SSID="${1:?usage: join-cam.sh SSID PASSWORD [IFACE]}"
PSK="${2:?password required}"
IFACE="${3:-}"

rfkill unblock all 2>/dev/null || true

if [ -z "$IFACE" ]; then
    IFACE=$(iw dev 2>/dev/null | awk '/Interface/{print $2; exit}')
fi
[ -n "$IFACE" ] || { echo "no wireless interface found (iw dev empty)"; exit 1; }
echo "Using wireless interface: $IFACE"

ip link set "$IFACE" up
pkill -f "wpa_supplicant.*-i *$IFACE" 2>/dev/null || true
sleep 1

CONF=$(mktemp)
wpa_passphrase "$SSID" "$PSK" > "$CONF"
wpa_supplicant -B -i "$IFACE" -c "$CONF"

echo "Associating..."
for _ in $(seq 1 25); do
    state=$(wpa_cli -i "$IFACE" status 2>/dev/null | sed -n 's/^wpa_state=//p')
    [ -n "$state" ] && echo "  wpa_state=$state"
    [ "$state" = "COMPLETED" ] && break
    sleep 1
done

echo "Requesting DHCP lease on $IFACE..."
dhclient -v "$IFACE" || true
echo "--- $IFACE address ---"; ip -4 addr show "$IFACE"
echo "--- routes ---"; ip route
