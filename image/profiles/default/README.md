# `default` profile

The example profile. Ships exactly what the base recipe installs: an XFCE
desktop with lightdm, the boot-state reporter, FreeIPA client, Kerberos NFS
home directories. No applications beyond the desktop.

Use this profile as the starting point for a new variant: copy the directory,
rename it, and add packages or a `setup-chroot` script for whatever your
deployment needs.
