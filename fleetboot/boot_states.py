"""The ordered set of lifecycle states a booting machine reports back.

Keeping this in one place means the systemd units, the PAM hook, the reporter,
and the server all agree on the exact strings. Adding a new state is a single
edit here plus a trigger in the image.
"""

from enum import Enum


class BootState(str, Enum):
    """Lifecycle states a machine progresses through during a boot."""

    GRUB_RUNNING = "grub_running"
    KERNEL_LOADED = "kernel_loaded"
    INITRD_LOADED = "initrd_loaded"
    BOOTING_KERNEL = "booting_kernel"
    NETWORK_UP = "network_up"
    SCRATCH_MOUNTED = "scratch_mounted"
    NFS_MOUNTED = "nfs_mounted"
    LOGIN_READY = "login_ready"
    USER_LOGGED_IN = "user_logged_in"


# The order matters: each state must be reached before the next one is valid.
# The server uses this to reject out-of-order reports (a defence-in-depth check
# against a confused or tampered-with client).
BOOT_STATE_ORDER: tuple[BootState, ...] = (
    # GRUB-emitted (via cat over HTTP) — earliest visible lifecycle stages.
    BootState.GRUB_RUNNING,
    BootState.KERNEL_LOADED,
    BootState.INITRD_LOADED,
    BootState.BOOTING_KERNEL,
    # Image-side (reporter Python). After this point the kernel is up.
    BootState.NETWORK_UP,
    BootState.SCRATCH_MOUNTED,
    BootState.NFS_MOUNTED,
    BootState.LOGIN_READY,
    BootState.USER_LOGGED_IN,
)


def state_index(state: BootState) -> int:
    """Return the position of a state in the canonical order."""
    return BOOT_STATE_ORDER.index(state)
