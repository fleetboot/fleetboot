# `default` profile

The thinnest fleetboot image. The base recipe gives you a Debian
rootfs with `live-boot`, networking, systemd, the fleetboot
boot-state reporter, SSSD + krb5 + autofs for FreeIPA-secured NFS
homes, and the keytab-fetch + ipa-client-install machinery.
**No desktop, no display manager, no graphics drivers, no
applications.**

The default boots to a text console (`multi-user.target`).

```sh
make image PROFILE=default
```

## Example profiles you can layer on top

| Profile                | What it adds                              |
|------------------------|-------------------------------------------|
| `xfce-desktop`         | XFCE + LightDM + audio                    |
| `gnome-desktop`        | GNOME 3 + GDM                             |
| `kde-desktop`          | Plasma + SDDM                             |
| `cinnamon-desktop`     | Cinnamon + LightDM                        |
| `intel-graphics`       | Mesa + xserver-xorg-video-intel + microcode |
| `amd-graphics`         | Mesa + firmware-amd-graphics              |
| `nvidia-graphics`      | nvidia-driver from non-free, DKMS         |
| `ssh-debug`            | sshd + boot-time authorized_keys delivery |
| `logo`                 | Fleetboot-branded wallpaper for the greeter |

These are worked examples. You're expected to fork them, ignore
them, or replace them entirely — the resolver treats every profile
under `image/profiles/` the same regardless of origin.

## Inheritance

Each profile may declare one or more parents in a `parent` file (one
per line). The resolver linearises the chain, de-duplicates shared
ancestors, unions `extra-packages.list`, layers `overlay/` (child
wins), and concatenates `setup-chroot` scripts in order.

To compose your own:

```
image/profiles/my-fleet/parent
----------------------------------
cinnamon-desktop
nvidia-graphics
----------------------------------
```

```sh
make image PROFILE=my-fleet
```
