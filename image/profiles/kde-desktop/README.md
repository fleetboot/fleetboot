# `kde-desktop` profile

The base fleetboot image plus KDE Plasma + SDDM. Adds a Plasma desktop
session behind an SDDM login screen.

```sh
make image PROFILE=kde-desktop
```

KDE is highly configurable per-user — useful when teachers want their
own workspace setups, but the per-user state needs to live on NFS
homes (the immutable root forgets every reboot).
