# `intel-graphics` profile

Mix-in profile for Intel integrated GPUs. The `i915` kernel module
ships with `linux-image` by default; this profile adds the userspace
DDX driver, Mesa (GL + Vulkan + DRI), CPU microcode updates, and
firmware blobs that newer chipsets need.

Designed to **stack with a desktop profile**, not stand alone:

```
image/profiles/cinnamon-desktop/parent
    intel-graphics
    amd-graphics
```

(The shipped `cinnamon-desktop`, `xfce-desktop`, `gnome-desktop`, and
`kde-desktop` profiles all inherit from both `intel-graphics` and
`amd-graphics` so an admin doesn't have to worry about which GPU is
in any given machine.)

## Covers

- Eaglelake (G41, GMA X4500, ~2008) → current Iris Xe.
- Both classic intel DDX (`xserver-xorg-video-intel`) and the
  kernel `modesetting` driver in `xserver-xorg-core`. The former
  gives better behaviour on older chipsets; the latter is the
  default on modern ones.

## Caveats

- The legacy `xserver-xorg-video-intel` DDX is unmaintained upstream.
  If you target only modern Intel hardware (Ivy Bridge and newer),
  you can omit it via a derived profile — `modesetting` is enough.
- Firmware blob is in Debian's `non-free-firmware` component, which
  trixie enables by default.
