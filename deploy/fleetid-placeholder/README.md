# fleetid placeholder

This directory is empty on purpose, and exists so that a Docker bind
mount always has something to point at.

The `fleetboot` service mounts `${FLEETID_DIR}` at `/fleetid` inside the
container, and this directory is the default. Deployments that don't use
[`fleetid`](https://github.com/fleetboot/fleetid) get an empty, harmless
mount — rather than Docker silently creating a stray `../fleetid`
directory next to their checkout, or the container refusing to start
because the path doesn't exist.

## When you do use fleetid

Set `FLEETID_DIR` in fleetboot's `.env` to your fleetid checkout — an
absolute path is clearest:

```
FLEETID_DIR=/opt/fleetboot/fleetid
```

That makes fleetid's composable `freeipa` image profile resolve for
builds started from the dashboard or MCP, which run `make image` inside
the container. A deployment wires that profile in by symlinking it:

```
image/profiles/freeipa -> ../../../fleetid/profile/freeipa
```

Three levels up from `/app/image/profiles` inside the container is `/`,
so the link lands on `/fleetid/profile/freeipa` — which is exactly where
the mount puts it. Nothing about the symlink needs to change.

Without it, `make image` run from a shell on the host still works (the
symlink resolves against the real sibling checkout there), but the same
build started from the dashboard or MCP fails with
`profile not found: 'freeipa'`.
