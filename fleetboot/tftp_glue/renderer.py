"""Wires the Fleetboot client + grub-template into the ``AssetRenderer``
seam that ``policy.Policy`` accepts.

The renderer is the thing the policy layer calls once a request has been
authorised. We:

  1. Mint a fresh per-boot session token from fleetboot (bound to the MAC),
  2. render a per-MAC ``grub.cfg`` that stamps that token into every URL,
  3. return it as bytes (UTF-8) so the TFTP server can DATA-block it out.

We only authorise filenames that look like grub configs (``grub.cfg`` plus a
narrow allowlist of GRUB's standard fallbacks). Anything else returns
``None``, which the policy layer treats as deny.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from tftpjail.identity import ClientIdentity

from .client import FleetbootClient
from .grub_template import render_grub_cfg


def build_grub_config_renderer(
    *,
    fleetboot_client: FleetbootClient,
    fleetboot_base_url: str,
) -> Callable[[Any, ClientIdentity, str], Optional[bytes]]:
    """Return an ``AssetRenderer`` callable compatible with ``Policy``.

    The ``profile`` argument is ignored at this stage — the registry just
    has to say "this MAC is known" for us to render. Per-profile policy
    (different images, different boot dirs) plugs in here later.

    The request path that reaches us has already been validated as a
    ``/jail/<mac>/<arch>/<platform>/<uuid?>`` identity by the policy layer,
    so we render unconditionally: the response *is* the per-MAC grub.cfg.
    """

    def render(
        profile: Any, identity: ClientIdentity, request_filename: str
    ) -> Optional[bytes]:
        token = fleetboot_client.mint_session(identity.asserted_mac)
        # The policy layer hands us back whatever the registry returned for
        # this MAC. We read its `serial_console` flag, `profile_name`, and
        # `architecture` so we render kernel cmdline + squashfs target
        # against what's actually been built for the machine, not against
        # what the booting GRUB happens to be (a BIOS-PXE'd x86_64 box
        # reports identity.architecture='i386' even though we want the
        # amd64 squashfs).
        serial_console = bool(getattr(profile, "serial_console", False))
        profile_name = getattr(profile, "profile_name", "default") or "default"
        registered_arch = getattr(profile, "architecture", None)
        scratch_mode = getattr(profile, "scratch_mode", None) or "volatile"
        body = render_grub_cfg(
            identity=identity,
            fleetboot_base_url=fleetboot_base_url,
            boot_token=token,
            profile=profile_name,
            serial_console=serial_console,
            target_architecture=registered_arch,
            scratch_mode=scratch_mode,
        )
        return body.encode("utf-8")

    return render
