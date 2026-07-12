# `freeipa-desktop` profile

A concrete deployment profile: a Cinnamon desktop that logs in against
FreeIPA and mounts each user's Kerberos-secured NFSv4 home. It composes:

```
freeipa-desktop
├── parent: cinnamon-desktop   # Cinnamon + LightDM + intel/amd graphics + logo
└── parent: freeipa            # FreeIPA/SSSD/Kerberos/NFS/autofs identity
                               #   (the fleetid project, symlinked in at
                               #   image/profiles/freeipa)
```

## Static identity (why, and what's baked)

`ipa-client-install` **rotates the host key on every run**, which is fatal
for an immutable netboot image: each boot is a fresh tmpfs, so a one-time
enrolment keytab is single-use and every boot after the first fails to
re-enrol. So instead of enrolling per boot, this profile bakes a **complete,
already-enrolled identity** and the `freeipa` profile's `enroll-freeipa` runs
in *static mode* — it does NOT run `ipa-client-install`; it just starts SSSD +
`rpc-gssd` and points autofs at the NFS server. Every boot is identical and
survives reboots.

The baked identity lives in `overlay/`:

- `etc/krb5.keytab` — the host's permanent Kerberos key. **Secret**;
  gitignored (see the repo `.gitignore`). Provision it once from FreeIPA
  (`ipa-getkeytab -p host/<fqdn>`) — never rotate it afterward or the image
  goes stale.
- `etc/sssd/sssd.conf`, `etc/ipa/default.conf`, `etc/ipa/ca.crt`,
  `etc/krb5.conf`, `etc/nsswitch.conf`, `etc/pam.d/common-*` — the client
  config a successful `ipa-client-install` produces (captured once, then
  baked). Not secret.
- `etc/fleetboot/identity.conf` — realm/domain/server + `IPA_NFS_SERVER`
  (substituted into the autofs maps at boot) + `IPA_HOSTNAME`.
- `etc/hosts` — resolves `ipa.fleet.lan` / `nfs.fleet.lan` to the fleetboot
  host, since this flat LAN has no DNS zone for the realm.
- `extra-packages.list` — `firefox-esr`.

Build: `make image PROFILE=freeipa-desktop`. The machine PXE-boots to a
Cinnamon greeter; a FreeIPA user (e.g. `matt`) logs in and autofs mounts the
Kerberos NFSv4 home at `/home/<user>` over `sec=krb5p`.

## Caveats

Static identity means **every machine booting this image shares the host
principal** (`laptop.fleet.lan`) — fine for a single laptop, not for a fleet.
Per-machine identity would need fleetboot to mint a fresh per-MAC keytab each
boot via its `/enrol` rail. This profile is specific to one deployment (the
IP, realm, and baked key are concrete) — treat it as a worked example to copy,
not a generic shipped profile.
