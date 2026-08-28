#!/usr/bin/env bash
# Block until an AP-capable Wi-Fi phy is present, so the Mystery Gift service
# does not start before the USB adapter has enumerated after boot. Used as the
# systemd unit's ExecStartPre (kept in a script so systemd's Exec parser never
# has to interpret the inline shell escapes). Exits 0 as soon as one is found,
# or 1 after the timeout (systemd then retries the unit).
#
# Usage: wait_for_ap_phy.sh [timeout_seconds]   (default 60)
set -u

timeout=${1:-60}
for ((i = 0; i < timeout; i++)); do
    for phy_dir in /sys/class/ieee80211/phy*; do
        [ -e "$phy_dir" ] || continue
        phy=$(basename "$phy_dir")
        if iw phy "$phy" info 2>/dev/null | grep -qE '^[[:space:]]*\* AP$'; then
            exit 0
        fi
    done
    sleep 1
done
exit 1
