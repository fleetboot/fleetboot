# logo profile

Fleet-branded wallpaper for the login greeter.

Drops a 1920x1080 PNG into `/usr/share/backgrounds/fleetboot/` and
wires whichever display-manager greeter is installed at chroot time
(lightdm-gtk-greeter, gdm3, sddm) to use it as the login background.
The same file is set as the default per-user desktop wallpaper via
the relevant configuration mechanism (Cinnamon's gsettings schema
override, etc.).

This profile sits between the base image and any desktop-environment
profile in the inheritance chain — desktop profiles list `logo` as
their parent, and the resolver dedupes shared ancestors so admins
can compose `logo + intel-graphics + ssh-debug + cinnamon-desktop`
without writing override files.

If you don't want fleet branding on a particular profile, drop
`logo` from that profile's `parent` file.
