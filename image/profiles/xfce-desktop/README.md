# `xfce-desktop` profile

The base fleetboot image plus XFCE 4 + LightDM. Adds a graphical login
screen and a desktop session.

Profile content:

- `extra-packages.list`: `xfce4`, `xfce4-goodies`, `lightdm`,
  `lightdm-gtk-greeter`, `xserver-xorg`, `xkb-data`, plus PipeWire for
  audio.
- `setup-chroot`: flips the systemd default to `graphical.target` so
  the image boots into LightDM.

Combine with `school` (which declares `parent: xfce-desktop`) to get a
locked-down student-facing image.

## Build

```sh
make image PROFILE=xfce-desktop
```

Produces `build/fleetboot-xfce-desktop-amd64.squashfs`.

## Why XFCE specifically

XFCE is the lightest of the four "full desktop" choices we ship
examples for — small RAM footprint, fast on older hardware,
straightforward to lock down for a classroom. The other choices
(`gnome-desktop`, `kde-desktop`, `cinnamon-desktop`) follow the same
shape if you'd prefer one of them as the starting point.
