# `default` profile

The thinnest fleetboot image. The base recipe gives you a Debian rootfs
with `live-boot`, networking, systemd, the fleetboot boot-state
reporter, SSSD + krb5 + autofs for FreeIPA-secured NFS homes, and the
keytab-fetch + ipa-client-install machinery. **No desktop, no display
manager, no graphics drivers, no applications.**

The default boots to a text console (`multi-user.target`).

```sh
make image PROFILE=default
```

## Layering on top

The other profiles in this directory show what's worth adding:

| Profile                | What it stacks on top                     |
|------------------------|-------------------------------------------|
| `xfce-desktop`         | XFCE + LightDM + audio                    |
| `gnome-desktop`        | GNOME 3 + GDM                             |
| `kde-desktop`          | Plasma + SDDM                             |
| `cinnamon-desktop`     | Cinnamon + LightDM                        |
| `amd-graphics`         | Mesa + firmware-amd-graphics              |
| `nvidia-graphics`      | nvidia-driver from non-free, DKMS         |
| `school`               | `parent: xfce-desktop` + LibreWolf        |

## Inheritance

Each profile may declare one or more parents in a `parent` file (one
per line). The resolver linearises the chain, de-duplicates shared
ancestors, unions `extra-packages.list`, layers `overlay/` (child
wins), and concatenates `setup-chroot` scripts in order.

To compose your own:

```
image/profiles/teacher/parent
----------------------------------
school
nvidia-graphics
----------------------------------
```

```sh
make image PROFILE=teacher
```
