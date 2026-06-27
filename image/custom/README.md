# Admin customisation of the Fleetboot image

Fleetboot ships a base squashfs recipe at `image/recipes/fleetboot-base.yaml`.
**Do not edit that file.** Customise the image through the four contract points
below — they all live in this directory and are picked up automatically by
`make image`.

## 1. Add extra Debian packages

Edit `extra-packages.list`. One package per line. Lines starting with `#` and
blank lines are ignored.

```text
# extra apps installed alongside the base image
firefox-esr
libreoffice
```

## 2. Overlay files into the image

Drop any files you want shipped in the image under `overlay/`. The directory
tree is copied verbatim into the image root, so `overlay/etc/foo.conf` lands at
`/etc/foo.conf` on every booted machine.

This is the right place for site-local configuration, wallpapers, certificates,
extra shell scripts in `/usr/local/bin/`, and so on.

## 3. Hook scripts at known points

Optional shell scripts run at fixed moments in the build. Make them executable
(`chmod +x`) and they will run; leave them absent and the build skips them.

| File | When it runs | Working dir | Env it gets |
|------|--------------|-------------|-------------|
| `hooks/pre-build` | After the base rootfs is installed, before our reporter is added. | Host. | `ROOTDIR` = image root; `ARTIFACTDIR` = where outputs land. |
| `hooks/post-build` | After everything else, before the squashfs is packed. | Host. | Same. |

Hooks run on the host, not inside the image. To run things *inside* the image,
chroot from the hook (`chroot "$ROOTDIR" /bin/sh -c ...`).

## 4. Extra debos actions

If the four points above are not enough, drop a `local.yaml` containing extra
debos actions. The base recipe includes it (with the `recipe` action) right
before the squashfs is packed, so anything you add runs near the end.

```yaml
# image/custom/local.yaml
architecture: amd64
actions:
  - action: run
    description: "site-local extra step"
    chroot: true
    command: echo hello > /etc/site-marker
```

## Testing

The fast structural tests in `make test` verify that this contract exists and
that the base recipe still wires it correctly. The slow `make image-smoke`
actually builds and boots the image to prove it works.
