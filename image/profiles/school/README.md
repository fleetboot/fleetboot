# `school` profile

The base fleetboot image (XFCE desktop, FreeIPA-backed identity, Kerberos
NFSv4 home directories, the boot-state reporter) plus **LibreWolf** as the
default web browser. Intended for classroom desktops and student labs.

## Why LibreWolf

LibreWolf is a Firefox fork that ships with telemetry, ads, and tracking
disabled by default, and it has a stable Debian apt repository. For a
locked-down student desktop those defaults are the right ones.

## How it lands in the image

`extra-packages.list` brings in [`extrepo`](https://salsa.debian.org/extrepo-team/extrepo),
a tiny Debian tool that registers third-party apt repositories from a curated
list (including the maintainer-signed key). `setup-chroot` then runs
`extrepo enable librewolf && apt-get install librewolf` inside the image
build, and sets LibreWolf as the default `x-www-browser`.

## Build

```sh
make image PROFILE=school
```

Produces `build/fleetboot-school-amd64.squashfs`.

## Enrol machines into this profile

```
curl -X POST http://fleetboot.example.internal/machines \
  -H "Authorization: Bearer $ADMIN_SECRET" \
  -d '{
    "mac": "aa:bb:cc:dd:ee:ff",
    "profile_name": "school",
    "architecture": "x86_64",
    "platform": "efi"
  }'
```

The `profile_name` here is what tftpjail's renderer stamps into the kernel
cmdline, so the booted machine fetches `fleetboot-school-amd64.squashfs`
instead of the default.

## Customising the login & desktop background

Two things need to be set: the **LightDM greeter** background (what users
see at the login screen) and the **XFCE desktop** background (what they
see once logged in). Both can be done entirely inside the profile.

### 1. Stage the wallpaper into the image

Drop the image file under the profile overlay so it lands at a stable
path inside the squashfs:

```
image/profiles/school/overlay/usr/share/backgrounds/fleetboot-default.png
```

Any PNG or JPEG works. `1920x1080` is a sensible default; XFCE will
scale-to-fit either way. The overlay tree is copied as-is, so file
modes and ownership of the staged file land identically in the image.

### 2. Configure the LightDM greeter

Drop a config snippet into the same overlay:

```
image/profiles/school/overlay/etc/lightdm/lightdm-gtk-greeter.conf.d/50-fleetboot.conf
```

with contents:

```ini
[greeter]
background = /usr/share/backgrounds/fleetboot-default.png
theme-name = Adwaita-dark
icon-theme-name = Adwaita
font-name = Cantarell 11
```

LightDM merges everything in `lightdm-gtk-greeter.conf.d/` over the
defaults — no need to ship the full config.

### 3. Configure the XFCE desktop background

XFCE reads per-user wallpaper settings from xfconf at login, but it
also honours system-wide defaults that get copied into a fresh user's
profile. Drop:

```
image/profiles/school/overlay/etc/xdg/xfce4/xfconf/xfce-perchannel-xml/xfce4-desktop.xml
```

with contents:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<channel name="xfce4-desktop" version="1.0">
  <property name="backdrop" type="empty">
    <property name="screen0" type="empty">
      <property name="monitor0" type="empty">
        <property name="workspace0" type="empty">
          <property name="last-image" type="string"
                    value="/usr/share/backgrounds/fleetboot-default.png"/>
          <property name="image-style" type="int" value="5"/>
        </property>
      </property>
    </property>
  </property>
</channel>
```

`image-style=5` is "zoomed" — fits the screen, crops slight overhang.

### 4. Rebuild

```sh
make image PROFILE=school
```

That's the whole loop. Existing machines pick up the new background on
their next reboot — the dashboard's build-version column flips from
green to orange on stale machines until they reboot.

### Per-classroom backgrounds

If different classrooms want different wallpapers, create one profile
per classroom (`school-room-a`, `school-room-b`, ...) and use the
same recipe with a different overlay PNG. Enrol each MAC into its
classroom's profile, or set up an auto-enrol rule keyed on the
classroom's IP CIDR.
