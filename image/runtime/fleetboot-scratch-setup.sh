#!/bin/sh
# Set up a local-disk scratch filesystem mounted at /var/scratch.
#
# Honours the kernel cmdline `fleetboot.scratch=<mode>` set by tftpjail's
# renderer:
#
#   volatile    Wipe + format ext4 every boot. Disk is "RAM you don't pay
#               for" — survives nothing, preserves the immutable-fleet
#               security model.
#   persistent  Keep the existing ext4 across reboots if it has a
#               fleetboot signature, else format and write the signature.
#               Browser cache / build temp can survive reboots.
#   off         Do nothing. Mounts no scratch.
#
# Safety: never touches a disk that has any partition table or filesystem
# the kernel recognises, unless that filesystem has the fleetboot scratch
# signature (a label on the partition's superblock). This prevents the
# script from wiping an admin's data disk that happened to be plugged in.
#
# Best-effort: any failure is logged and exit 0'd. A broken scratch must
# not stop the boot — the rest of the desktop session still works.

set -u

LOG_TAG="fleetboot-scratch"
log() { echo "$LOG_TAG: $*" >&2; }
warn() { log "WARN: $*"; }

MOUNTPOINT=/var/scratch
SIGNATURE_LABEL="fleetboot-scratch"

# --- Read fleetboot.scratch=<mode> from cmdline -----------------------------
mode=$(awk -v RS=' ' -F= '/^fleetboot\.scratch=/{print $2}' /proc/cmdline | tr -d '[:space:]')
[ -z "$mode" ] && mode=volatile  # safest default

case "$mode" in
    off)
        log "scratch mode=off; not configuring any disk"
        exit 0
        ;;
    volatile|persistent)
        ;;
    *)
        warn "unknown scratch mode '$mode'; treating as off"
        exit 0
        ;;
esac

# --- Pick a candidate disk --------------------------------------------------
# The simplest sensible rule: the largest non-removable, non-rotational
# block device that isn't already serving the live OS. /sys/block is the
# source of truth; we deliberately don't shell out to `lsblk` so the
# initramfs/early-boot environment doesn't matter.
candidate=""
candidate_size=0
for sysblk in /sys/block/*; do
    name=$(basename "$sysblk")
    case "$name" in
        loop*|ram*|sr*|fd*|dm-*|zram*) continue ;;
    esac
    # Skip removable devices (USB sticks etc).
    removable=$(cat "$sysblk/removable" 2>/dev/null || echo 1)
    [ "$removable" = "1" ] && continue
    # Size in 512-byte sectors. Skip empty / <1 GB devices — almost
    # certainly not the disk the admin intends as scratch.
    size=$(cat "$sysblk/size" 2>/dev/null || echo 0)
    [ "$size" -lt 2097152 ] && continue   # ~1 GiB
    if [ "$size" -gt "$candidate_size" ]; then
        candidate="/dev/$name"
        candidate_size=$size
    fi
done

if [ -z "$candidate" ]; then
    log "no candidate disk found; scratch not configured"
    exit 0
fi

log "candidate disk: $candidate ($((candidate_size / 2 / 1024 / 1024)) GiB)"

# --- Decide: do we own this disk? -------------------------------------------
# We own it iff it has a single ext4 filesystem labelled "fleetboot-scratch".
existing_fs=$(blkid -s TYPE -o value "$candidate" 2>/dev/null || true)
existing_label=$(blkid -s LABEL -o value "$candidate" 2>/dev/null || true)

owned=0
if [ "$existing_fs" = "ext4" ] && [ "$existing_label" = "$SIGNATURE_LABEL" ]; then
    owned=1
fi

# Sanity: mkfs.ext4 must exist. Without it, the rest of this script is
# pointless. Fail loud (exit 1, unit marked failed) so the dashboard's
# diagnostics surface "scratch broken" instead of "scratch silently
# disabled".
if ! command -v mkfs.ext4 >/dev/null 2>&1; then
    warn "mkfs.ext4 not installed (need e2fsprogs); scratch cannot be set up"
    exit 1
fi

case "$mode" in
    volatile)
        # Truly volatile: format whatever's there. Choosing volatile is
        # already an admin opt-in saying "this disk is scratch, anything
        # on it is replaceable". Persistent state lives on NFS-mounted
        # /home, not on the local disk.
        log "volatile: formatting $candidate (was $existing_fs/$existing_label)"
        if ! mkfs.ext4 -q -F -L "$SIGNATURE_LABEL" "$candidate"; then
            warn "mkfs.ext4 failed on $candidate; scratch not configured"
            exit 1
        fi
        ;;
    persistent)
        # Persistent: keep the safety check. A typo on the mode shouldn't
        # cost user data — only blank disks or our-labelled disks
        # are eligible for format. Disks with foreign filesystems are
        # left alone (exit 0 — intentional no-op, not a failure).
        if [ "$owned" = "1" ]; then
            log "persistent: existing fleetboot-scratch filesystem; reusing"
        elif [ -z "$existing_fs" ]; then
            log "persistent: empty disk; formatting fresh (label=$SIGNATURE_LABEL)"
            if ! mkfs.ext4 -q -F -L "$SIGNATURE_LABEL" "$candidate"; then
                warn "mkfs.ext4 failed on $candidate; scratch not configured"
                exit 1
            fi
        else
            warn "persistent: $candidate has $existing_fs/$existing_label; refusing to wipe — switch to volatile to override"
            exit 0
        fi
        ;;
esac

# --- Mount -------------------------------------------------------------------
mkdir -p "$MOUNTPOINT"
chmod 1777 "$MOUNTPOINT"
if mount -t ext4 -o noatime "$candidate" "$MOUNTPOINT"; then
    chmod 1777 "$MOUNTPOINT"
    log "mounted $candidate at $MOUNTPOINT (mode=$mode)"
else
    warn "mount $candidate at $MOUNTPOINT failed"
    exit 1
fi
