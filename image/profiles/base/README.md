# `base` profile

The **implicit root of the profile tree**. Every other profile inherits
from `base` automatically — you don't (and shouldn't) list it in a
`parent` file. Its `extra-packages.list`, `overlay/`, and `setup-chroot`
are merged into *every* image, ahead of everything else.

Two consequences follow, and they are the whole point of it existing:

1. **One place for fleet-wide additions.** A package or overlay file you
   want on *every* machine regardless of profile goes here, instead of
   being copy-pasted into each profile.
2. **Building `base` rebuilds the whole fleet.** Because every profile is
   downstream of `base`, `start_build` on `base` cascades a rebuild to
   every profile (root first, then each descendant). That is how you
   propagate a change to shared image material — including a change to
   the recipe (`image/fleetboot-base.yaml`) or the shared runtime
   (`image/runtime/`, `image/systemd/`), which affects every image but
   isn't otherwise modelled as inheritance.

`base` is deliberately near-empty. The heavy lifting of the base image —
the Debian bootstrap, the fleetboot reporter/heartbeat/scratch runtime,
the systemd units — lives in the recipe `image/fleetboot-base.yaml`, not
here. This profile is the inheritance *root* and the rebuild *lever*, not
a second copy of the base.

## Building

```sh
make image PROFILE=base           # the bare base image (rarely booted directly)
```

From the dashboard or MCP, `start_build` on `base` rebuilds `base` and
then every other profile in turn.
