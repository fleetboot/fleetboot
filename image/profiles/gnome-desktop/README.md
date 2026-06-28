# `gnome-desktop` profile

The base fleetboot image plus GNOME 3 + GDM. Adds a GNOME shell session
behind a GDM login screen.

```sh
make image PROFILE=gnome-desktop
```

GNOME is the heaviest of the four desktop options we ship — pick this
if you want the modern default Debian/Ubuntu desktop experience and
have the RAM (4 GB+) and recent-ish hardware to support it.
