"""FastAPI app: receives boot-state reports, mints sessions, and serves the
boot assets (kernel, initrd, squashfs) to GRUB and live-boot.

Three endpoint groups, each with a different threat model:

  POST /status        — image-side reporter posts lifecycle state.
                        Auth: per-boot session token (Bearer).
  POST /sessions      — tftpjail mints a per-boot session token to stamp
                        into the rendered grub.cfg.
                        Auth: shared secret (Bearer) — only tftpjail.
  GET  /boot/<file>   — GRUB / live-boot fetches a kernel, initrd, or
                        squashfs.
                        Auth: per-boot session token in `?t=` query, since
                        bootloaders generally cannot set headers.

For unknown tokens, malformed input, or out-of-order states we return uniform
error responses — the boot network is hostile by default, so we do not leak
which-token-exists information to probers.
"""

from __future__ import annotations

import hmac
import re
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from fleetboot.boot_states import BootState
from fleetboot.server.boot_sessions import (
    BootSessionStore,
    OutOfOrderStateError,
    UnknownTokenError,
)
from fleetboot.server.registry import AutoEnrolRule, Machine, MachineRegistry


# Hostnames the reporter sometimes sends that aren't useful for display:
# the live-boot initramfs default, the host-of-build leakage (debos
# fakemachine), and various stand-ins. The reporter inside the image has
# its own filter but we belt-and-braces on the server side too so a fix
# doesn't require an image rebuild to take effect.
_BORING_HOSTNAMES = frozenset({
    "", "localhost", "(none)", "none",
    "debian", "debian-live", "fakemachine",
    "fleetboot", "fleetboot-client",
})


# Static filenames we always serve under /boot/.
_STATIC_BOOT_FILES = frozenset({"vmlinuz", "initrd.img"})

# Profiled squashfs names. The image build produces
# `fleetboot-<profile>-<arch>.squashfs` and we accept any name of that
# shape. The file still has to exist on disk in the configured boot_dir,
# so this is not an enumeration oracle for what was built.
_PROFILED_SQUASHFS = re.compile(
    r"^fleetboot-[a-z0-9][a-z0-9-]*-(amd64|arm64|i386)\.squashfs$"
)


def is_allowed_boot_filename(filename: str) -> bool:
    """Whitelist check applied before any filesystem operation."""
    if filename in _STATIC_BOOT_FILES:
        return True
    return _PROFILED_SQUASHFS.fullmatch(filename) is not None


# Kept for tests that still want a concrete set of the static names.
ALLOWED_BOOT_FILES = _STATIC_BOOT_FILES


class StatusReport(BaseModel):
    """The payload a machine sends to /status."""

    state: BootState = Field(
        ...,
        description="The lifecycle state the machine has just entered.",
    )
    # Optional details — the user_logged_in trigger passes the username here.
    # We never trust this for authorisation, only display.
    detail: Optional[str] = Field(
        default=None, max_length=256, description="Optional human-readable detail."
    )
    # Hostname the booted image has settled on (from DHCP/DNS, or the
    # image's own /etc/hostname). Used purely for human-readable display in
    # the dashboard — never trusted for authorisation.
    hostname: Optional[str] = Field(
        default=None, max_length=253,
        description="Hostname as the booted image sees it.",
    )
    # Build version of the squashfs the machine is currently running.
    # The reporter reads it from /etc/fleetboot/build-version. The
    # dashboard colours rows green when this matches the latest sidecar
    # version on the server, orange when it doesn't (machine needs a
    # reboot to pick up the new image).
    boot_version: Optional[str] = Field(
        default=None, max_length=128,
        description="Image build version stamp from /etc/fleetboot/build-version.",
    )
    diagnostics: Optional[str] = Field(
        default=None, max_length=8192,
        description=(
            "Optional snapshot of what's broken (systemctl --failed, "
            "display-manager state, recent journal). Server stores the "
            "latest non-empty value on the machine row."
        ),
    )
    hardware: Optional[dict] = Field(
        default=None,
        description=(
            "Hardware inventory: cpu_model, cpu_count, mem_total_mb, "
            "mem_available_mb, disks[], mounts[]. Stored as JSON; the "
            "dashboard parses for display. No server-side validation of "
            "the inner shape — best-effort telemetry."
        ),
    )


class StatusAcknowledgement(BaseModel):
    """What the server returns on a successful report."""

    ok: bool = True
    mac: str
    state: BootState


class MintRequest(BaseModel):
    """tftpjail's request to mint a per-boot session token."""

    mac: str = Field(..., description="MAC address the token should bind to.")


class MintResponse(BaseModel):
    """What the server returns on a successful mint."""

    token: str
    mac: str


class MachineEnrolment(BaseModel):
    """Body of POST /machines — an admin registering a fleet machine."""

    mac: str = Field(..., description="MAC address to register.")
    profile_name: str = Field(
        ..., description="Logical profile (image+policy) the machine belongs to."
    )
    architecture: str = Field(
        ..., description="CPU architecture: x86_64, arm64, or i386."
    )
    platform: str = Field(..., description="Firmware platform: efi or pc.")
    # Off by default — real student desktops do not have or need a serial.
    # Tests and headless lab boxes opt in.
    scratch_mode: str = Field(
        default="volatile",
        description=(
            "Local-disk scratch behaviour: 'volatile' (wipe + reformat every "
            "boot, default), 'persistent' (kept across reboots), or 'off' "
            "(ignore local disk entirely)."
        ),
    )
    reboot_command: Optional[str] = Field(
        default=None,
        description=(
            "Free-text shell command run on the fleetboot host when an "
            "admin clicks 'delete + reboot' on this machine. shell=True; "
            "admin owns the contents."
        ),
    )
    serial_console: bool = Field(
        default=False,
        description=(
            "If true, the renderer adds console=ttyS0 to the kernel cmdline. "
            "Enable for VMs and headless hardware; leave off for desktops."
        ),
    )


class MachineRecord(BaseModel):
    """One machine row as returned by /machines."""

    mac: str
    profile_name: str
    architecture: str
    platform: str
    serial_console: bool
    enrolled_by: str = "manual"
    hostname: Optional[str] = None
    hostname_seen_at: Optional[str] = None
    scratch_mode: str = "volatile"
    reboot_command: Optional[str] = None
    created_at: str

    @classmethod
    def from_machine(cls, machine: Machine) -> "MachineRecord":
        return cls(
            mac=machine.mac,
            profile_name=machine.profile_name,
            architecture=machine.architecture,
            platform=machine.platform,
            serial_console=machine.serial_console,
            enrolled_by=machine.enrolled_by,
            hostname=machine.hostname,
            hostname_seen_at=machine.hostname_seen_at,
            scratch_mode=machine.scratch_mode,
            reboot_command=machine.reboot_command,
            created_at=machine.created_at,
        )


class AutoEnrolRuleRequest(BaseModel):
    """Body of POST /auto-enrol-rules."""

    name: str
    match_kind: str = Field(..., description="'mac_prefix' or 'ip_cidr'")
    match_value: str
    profile_name: str
    architecture: str = "x86_64"
    # `any` (the default for new rules) matches every URL platform —
    # restores the old behaviour. `efi`/`pc` make the rule fire only
    # for that firmware type, useful for per-platform serial_console
    # defaults in mixed-firmware subnets.
    platform: str = "any"
    serial_console: bool = False
    # Local-disk scratch behaviour applied to machines auto-enrolled by
    # this rule. See Machine.scratch_mode for the value semantics.
    scratch_mode: str = "volatile"


class AutoEnrolRuleRecord(BaseModel):
    id: int
    name: str
    match_kind: str
    match_value: str
    profile_name: str
    architecture: str
    platform: str
    serial_console: bool
    scratch_mode: str = "volatile"
    created_at: str

    @classmethod
    def from_rule(cls, rule: AutoEnrolRule) -> "AutoEnrolRuleRecord":
        return cls(
            id=rule.id, name=rule.name, match_kind=rule.match_kind,
            match_value=rule.match_value, profile_name=rule.profile_name,
            architecture=rule.architecture, platform=rule.platform,
            serial_console=rule.serial_console,
            scratch_mode=rule.scratch_mode,
            created_at=rule.created_at,
        )


def create_app(
    sessions: BootSessionStore | None = None,
    *,
    mint_secret: str | None = None,
    boot_dir: Path | None = None,
    registry: MachineRegistry | None = None,
    admin_secret: str | None = None,
    dashboard_repo_root: Path | None = None,
    keytabs_dir: Path | None = None,
    authorized_keys_path: Path | None = None,
) -> FastAPI:
    """Build the FastAPI app.

    `sessions` — inject an existing store. A fresh in-memory store is
        created if omitted.
    `mint_secret` — shared secret required on /sessions. If None, /sessions
        returns 503 (minting disabled).
    `boot_dir` — directory holding the build artifacts served by /boot/.
        If None, /boot/* returns 503 (boot serving disabled).
    `registry` — MachineRegistry instance. If None, /machines returns 503.
    `admin_secret` — shared secret required on /machines AND on the
        dashboard. If None, both return 503.
    `dashboard_repo_root` — path to the fleetboot repo (so the dashboard
        can read profiles and trigger `make image`). When None, the
        dashboard is not mounted.
    `keytabs_dir` — directory holding per-MAC FreeIPA enrolment keytabs at
        `<keytabs_dir>/<mac>.keytab`. Served via /enrol/{token}/keytab.
        If None, /enrol/* returns 503 (keytab delivery disabled).
    """
    store = sessions if sessions is not None else BootSessionStore()
    app = FastAPI(title="Fleetboot control plane")

    def get_store() -> BootSessionStore:
        return store

    # Expose runtime config on the app so tests can reach it without going
    # through dependency injection.
    app.state.sessions = store
    app.state.mint_secret = mint_secret
    app.state.boot_dir = boot_dir
    app.state.registry = registry
    app.state.admin_secret = admin_secret

    @app.post("/status", response_model=StatusAcknowledgement)
    def post_status(
        report: StatusReport,
        request: Request,
        authorization: str | None = Header(default=None),
        store: BootSessionStore = Depends(get_store),
    ) -> StatusAcknowledgement:
        token = _extract_bearer_token(authorization)
        try:
            session = store.record_state(token, report.state)
        except UnknownTokenError:
            # Uniform 401: do not distinguish "unknown token" from "missing".
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="unauthorised",
            )
        except OutOfOrderStateError as err:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=str(err),
            )
        # Persist the event to the registry's boot-event log if one is
        # attached. The session store is in-memory; this is what survives
        # restarts and what the dashboard's history view reads.
        if registry is not None:
            registry.log_boot_event(
                mac=session.mac, state=report.state.value, detail=report.detail,
            )
            # Server-side observed IP — more trustworthy than anything
            # the client could claim. /status arrives via a TCP socket;
            # request.client.host is the booted-machine's IP.
            if request.client is not None and request.client.host:
                registry.update_last_ip(
                    mac=session.mac, ip=request.client.host,
                )
            if report.hostname and report.hostname.lower() not in _BORING_HOSTNAMES:
                registry.update_hostname(
                    mac=session.mac, hostname=report.hostname,
                )
            if report.boot_version:
                registry.update_boot_version(
                    mac=session.mac, boot_version=report.boot_version,
                )
            if report.diagnostics:
                registry.update_diagnostics(
                    mac=session.mac, diagnostics=report.diagnostics,
                )
            if report.hardware:
                import json as _json
                registry.update_hardware(
                    mac=session.mac,
                    hardware_json=_json.dumps(report.hardware),
                )
        return StatusAcknowledgement(
            ok=True, mac=session.mac, state=report.state
        )

    @app.post(
        "/sessions",
        response_model=MintResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def mint_session(
        request: MintRequest,
        authorization: str | None = Header(default=None),
        store: BootSessionStore = Depends(get_store),
    ) -> MintResponse:
        if mint_secret is None:
            # No secret configured -> minting is administratively disabled.
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="minting not configured",
            )
        presented = _extract_bearer_token(authorization)
        # Constant-time comparison so timing does not leak the secret length.
        if not presented or not hmac.compare_digest(presented, mint_secret):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="unauthorised",
            )
        session = store.mint(request.mac)
        return MintResponse(token=session.token, mac=session.mac)

    @app.get("/boot/{token}/{filename}")
    def serve_boot_file(
        token: str,
        filename: str,
        store: BootSessionStore = Depends(get_store),
    ) -> FileResponse:
        """Token in the path (not the query string) so live-boot's URL parser
        sees the real file extension. live-boot's mount-http.sh determines
        the archive type by ``sed 's/.*\\.\\(.*\\)/\\1/'`` on the URL — a
        query string like ``?t=...`` would put the token AFTER the dot and
        the file would be unrecognised."""
        if boot_dir is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="boot serving not configured",
            )
        # Filename allowlist before any filesystem operation. Same wire-level
        # response for "unknown name" and "missing on disk" so probers cannot
        # enumerate what we have.
        if not is_allowed_boot_filename(filename):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="not found"
            )
        if store.lookup(token) is None:
            # Uniform 401: same as /status.
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="unauthorised",
            )
        path = boot_dir / filename
        if not path.is_file():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="not found"
            )
        return FileResponse(str(path), media_type="application/octet-stream")

    # ---- /machines admin API ---------------------------------------------

    def _require_admin(authorization: str | None) -> None:
        """Reject anything that isn't the admin shared secret."""
        if registry is None or admin_secret is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="registry not configured",
            )
        presented = _extract_bearer_token(authorization)
        if not presented or not hmac.compare_digest(presented, admin_secret):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="unauthorised",
            )

    @app.post(
        "/machines",
        response_model=MachineRecord,
        status_code=status.HTTP_201_CREATED,
    )
    def enroll_machine(
        body: MachineEnrolment,
        authorization: str | None = Header(default=None),
    ) -> MachineRecord:
        _require_admin(authorization)
        # registry is non-None: _require_admin only returns successfully when
        # it has been configured.
        if body.scratch_mode not in ("volatile", "persistent", "off"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="scratch_mode must be volatile/persistent/off",
            )
        machine = registry.enroll(  # type: ignore[union-attr]
            mac=body.mac,
            profile_name=body.profile_name,
            architecture=body.architecture,
            platform=body.platform,
            serial_console=body.serial_console,
            scratch_mode=body.scratch_mode,
        )
        return MachineRecord.from_machine(machine)

    @app.get("/machines", response_model=list[MachineRecord])
    def list_machines(
        authorization: str | None = Header(default=None),
    ) -> list[MachineRecord]:
        _require_admin(authorization)
        return [
            MachineRecord.from_machine(m)
            for m in registry.list_all()  # type: ignore[union-attr]
        ]

    @app.get("/machines/{mac}", response_model=MachineRecord)
    def get_machine(
        mac: str, authorization: str | None = Header(default=None),
    ) -> MachineRecord:
        _require_admin(authorization)
        machine = registry.lookup(mac)  # type: ignore[union-attr]
        if machine is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="not found"
            )
        return MachineRecord.from_machine(machine)

    @app.get("/resolve/{mac}", response_model=MachineRecord)
    def resolve_machine(
        mac: str,
        source_ip: Optional[str] = None,
        platform: Optional[str] = None,
        authorization: str | None = Header(default=None),
    ) -> MachineRecord:
        """Read-only registry lookup, authenticated with the mint secret.

        tftpjail uses this on every read-request to decide whether a MAC is
        known and which profile/arch it belongs to. We deliberately give
        tftpjail less than the full admin surface — it only reads.

        If the MAC isn't registered, we check the auto-enrol rules: if one
        matches this MAC (and optionally the source IP that tftpjail saw),
        we enrol the machine on the spot under that rule's profile and
        return it. Admins audit auto-enrolled rows via the `enrolled_by`
        column (set to `rule:<name>`).
        """
        if registry is None or mint_secret is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="resolve not configured",
            )
        presented = _extract_bearer_token(authorization)
        if not presented or not hmac.compare_digest(presented, mint_secret):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="unauthorised",
            )
        machine = registry.lookup(mac)
        if machine is None:
            rule = registry.find_matching_rule(
                mac, source_ip=source_ip, platform=platform,
            )
            if rule is not None:
                # URL platform wins over the rule's platform when both
                # are present — the URL is observed truth (the client's
                # firmware reported it), while the rule's platform is an
                # admin default that may not fit a mixed-firmware subnet.
                # Architecture stays from the rule: BIOS GRUB reports
                # `i386` even on x86_64 machines, so we can't infer the
                # real CPU arch from the URL.
                # URL platform wins. If absent (shouldn't happen — the
                # path parser requires it — but be defensive), fall back
                # to the rule's platform unless that's 'any', in which
                # case 'efi' is the safest modern default.
                if platform in {"pc", "efi"}:
                    effective_platform = platform
                elif rule.platform in {"pc", "efi"}:
                    effective_platform = rule.platform
                else:
                    effective_platform = "efi"
                machine = registry.enroll(
                    mac=mac,
                    profile_name=rule.profile_name,
                    architecture=rule.architecture,
                    platform=effective_platform,
                    serial_console=rule.serial_console,
                    enrolled_by=f"rule:{rule.name}",
                    scratch_mode=rule.scratch_mode,
                )
        if machine is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="not found"
            )
        return MachineRecord.from_machine(machine)

    @app.post(
        "/auto-enrol-rules",
        response_model=AutoEnrolRuleRecord,
        status_code=status.HTTP_201_CREATED,
    )
    def add_auto_enrol_rule(
        body: AutoEnrolRuleRequest,
        authorization: str | None = Header(default=None),
    ) -> AutoEnrolRuleRecord:
        _require_admin(authorization)
        if body.match_kind not in ("mac_prefix", "ip_cidr"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="match_kind must be 'mac_prefix' or 'ip_cidr'",
            )
        if body.platform not in ("any", "efi", "pc"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="platform must be 'any', 'efi', or 'pc'",
            )
        try:
            rule = registry.add_auto_enrol_rule(  # type: ignore[union-attr]
                name=body.name,
                match_kind=body.match_kind,
                match_value=body.match_value,
                profile_name=body.profile_name,
                architecture=body.architecture,
                platform=body.platform,
                serial_console=body.serial_console,
            )
        except ValueError as err:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(err)
            )
        return AutoEnrolRuleRecord.from_rule(rule)

    @app.get(
        "/auto-enrol-rules", response_model=list[AutoEnrolRuleRecord]
    )
    def list_auto_enrol_rules_api(
        authorization: str | None = Header(default=None),
    ) -> list[AutoEnrolRuleRecord]:
        _require_admin(authorization)
        return [
            AutoEnrolRuleRecord.from_rule(r)
            for r in registry.list_auto_enrol_rules()  # type: ignore[union-attr]
        ]

    @app.delete("/auto-enrol-rules/{rule_id}")
    def delete_auto_enrol_rule(
        rule_id: int,
        authorization: str | None = Header(default=None),
    ) -> Response:
        _require_admin(authorization)
        removed = registry.remove_auto_enrol_rule(rule_id)  # type: ignore[union-attr]
        if not removed:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="not found"
            )
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.delete("/machines/{mac}")
    def delete_machine(
        mac: str, authorization: str | None = Header(default=None),
    ) -> Response:
        _require_admin(authorization)
        removed = registry.remove(mac)  # type: ignore[union-attr]
        if not removed:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="not found"
            )
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.get("/grub-event/{token}/{state_str}")
    def grub_event(
        token: str, state_str: str,
        store: BootSessionStore = Depends(get_store),
    ) -> Response:
        """Boot-stage event from GRUB.

        GRUB can't HTTP-POST, but it can `cat (http,host:port)/path` —
        a GET. The renderer emits these between linux/initrd/boot
        commands so the dashboard sees grub_running / kernel_loaded /
        initrd_loaded / booting_kernel before the kernel takes over.

        Token-bound (the per-boot token is already in the rendered
        grub.cfg). Unknown token → 401. Unknown state → 400. State that
        goes backward → 409 (same protection as /status).

        Returns 200 with empty body so `cat` shows nothing on screen.
        """
        try:
            state = BootState(state_str)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="unknown state",
            )
        try:
            session = store.record_state(token, state)
        except UnknownTokenError:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="unauthorised",
            )
        except OutOfOrderStateError as err:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=str(err),
            )
        if registry is not None:
            registry.log_boot_event(
                mac=session.mac, state=state.value, detail=None,
            )
        # Return a single byte so GRUB's `cat` has something to read
        # without erroring. A bare newline shows as a blank line in
        # GRUB's output — visually a clean separator between echoes.
        #
        # `Connection: close` matters: BIOS GRUB's HTTP module reads
        # Content-Length bytes but then waits for the server to close
        # the TCP socket before declaring the fetch "done". Without
        # this header uvicorn keeps the socket open for keep-alive,
        # causing a ~30s timeout per grub-event call and adding ~2 min
        # to each boot. Eager close avoids that entire wait.
        return Response(
            status_code=status.HTTP_200_OK,
            content=b"\n",
            media_type="text/plain",
            headers={"Connection": "close"},
        )

    @app.get("/enrol/{token}/keytab")
    def serve_keytab(
        token: str,
        store: BootSessionStore = Depends(get_store),
    ) -> FileResponse:
        """Per-MAC FreeIPA enrolment keytab fetch.

        The booting machine reads the per-boot token from /proc/cmdline and
        fetches its own enrolment keytab. The token validates the request
        belongs to a current boot session, and the keytab filename on disk
        is keyed by MAC so we naturally only ever serve THIS machine's
        keytab to THIS boot session. Unknown token or no provisioned
        keytab -> 401 / 404; uniform errors keep this from being a
        scrape oracle.
        """
        if keytabs_dir is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="keytab delivery not configured",
            )
        session = store.lookup(token)
        if session is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="unauthorised",
            )
        # The keytab filename is the normalised MAC. The session's mac is
        # already normalised by BootSessionStore.mint(), so a direct join
        # is safe — no traversal possible from arbitrary input.
        path = keytabs_dir / f"{session.mac}.keytab"
        if not path.is_file():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="no keytab provisioned",
            )
        return FileResponse(str(path), media_type="application/octet-stream")

    @app.get("/enrol/{token}/authorized_keys")
    def serve_authorized_keys(
        token: str,
        store: BootSessionStore = Depends(get_store),
    ) -> FileResponse:
        """Deployment-wide SSH authorized_keys fetched at boot time.

        Same auth pattern as /enrol/<token>/keytab: a valid per-boot token
        is enough — the keys aren't per-machine secrets, they're the
        admin's pubkeys that grant root SSH on any fleetboot machine.
        We refuse if the token is invalid (so a random LAN host can't
        snoop the admin's keys) and 503 if the deployment didn't
        configure a keys file (most production deployments won't).
        """
        if authorized_keys_path is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="authorized_keys delivery not configured",
            )
        session = store.lookup(token)
        if session is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="unauthorised",
            )
        if not authorized_keys_path.is_file():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="no authorized_keys provisioned",
            )
        return FileResponse(
            str(authorized_keys_path), media_type="text/plain",
        )

    # ---- Dashboard --------------------------------------------------------
    #
    # Mounted only when caller provided a repo root, a registry, and an
    # admin secret. Otherwise the dashboard would have nothing useful to
    # show, so we just don't expose any of its routes.
    if (
        dashboard_repo_root is not None
        and registry is not None
        and admin_secret is not None
    ):
        from fleetboot.server.build_jobs import BuildJobManager
        from fleetboot.server.dashboard import build_dashboard_router

        builds = BuildJobManager(repo_root=dashboard_repo_root)
        dashboard_router = build_dashboard_router(
            registry=registry,
            sessions=store,
            profiles_root=dashboard_repo_root / "image" / "profiles",
            admin_secret=admin_secret,
            builds=builds,
            boot_dir=boot_dir,
        )
        app.include_router(dashboard_router)
        app.state.builds = builds

    return app


def _extract_bearer_token(header_value: str | None) -> str:
    """Pull the token out of an 'Authorization: Bearer <token>' header.

    Returns an empty string when missing or malformed; the lookup will then
    fail uniformly as 'unknown'.
    """
    if not header_value:
        return ""
    parts = header_value.strip().split(maxsplit=1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return ""
    return parts[1].strip()
