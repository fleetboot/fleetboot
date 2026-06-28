#!/bin/sh
# Re-report the machine's current boot state to fleetboot.
#
# The fleetboot-heartbeat.timer fires this every 2 minutes. Without
# heartbeats, the dashboard's "latest state" column would only update
# when the boot lifecycle advanced — and after a server restart the
# in-flight reports never resumed, leaving the dashboard frozen on
# whatever state was last seen.
#
# Best-effort: any failure here is silent. The reporter logs its own
# transport errors and the boot is unaffected.

set -eu

STATE_FILE=/run/fleetboot/current-state

if [ ! -f "$STATE_FILE" ]; then
    # No state has ever been reported (e.g. boot hasn't reached
    # network_up yet). Nothing to re-send.
    exit 0
fi

state=$(head -n1 "$STATE_FILE" | tr -d '[:space:]')
if [ -z "$state" ]; then
    exit 0
fi

# Reuse the reporter we already ship. It pulls server + token from
# /proc/cmdline and handles auth, retries, and the boring-hostname
# filter that's already there.
exec /usr/bin/python3 -m fleetboot.reporter.report "$state"
