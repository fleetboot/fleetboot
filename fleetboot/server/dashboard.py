"""Operational dashboard.

Tiny FastAPI + Jinja2 HTML UI for an administrator to see registered
machines, edit profiles, and trigger image builds.

Auth: HTTP Basic — the admin secret is the password, any username works.
This is intended for use inside a trusted admin network; for public
exposure put it behind a reverse proxy + your own SSO.
"""

from __future__ import annotations

import hmac
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates

from fleetboot.server.build_jobs import BuildAlreadyRunningError, BuildJobManager
from fleetboot.server.boot_sessions import BootSessionStore
from fleetboot.server.registry import MachineRegistry


# Templates and static files live alongside this module.
_HERE = Path(__file__).resolve().parent
TEMPLATES_DIR = _HERE / "templates"
STATIC_DIR = _HERE / "static"

_security = HTTPBasic(realm="fleetboot")


def _require_admin(
    admin_secret: str,
) -> "callable":
    """Return a FastAPI dependency that gates a route on admin_secret."""

    def _dep(
        credentials: HTTPBasicCredentials = Depends(_security),
    ) -> str:
        if admin_secret is None or not hmac.compare_digest(
            credentials.password or "", admin_secret
        ):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="unauthorised",
                headers={"WWW-Authenticate": 'Basic realm="fleetboot"'},
            )
        return credentials.username

    return _dep


def _safe_profile_dir(profiles_root: Path, name: str) -> Path:
    """Reject any name that escapes profiles_root, then return the dir."""
    # No slashes, no dots, no NULs; matches profile name conventions.
    if not name or not name.replace("-", "").isalnum():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="invalid profile name",
        )
    candidate = (profiles_root / name).resolve()
    try:
        candidate.relative_to(profiles_root.resolve())
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="invalid profile name",
        )
    return candidate


def build_dashboard_router(
    *,
    registry: MachineRegistry,
    sessions: BootSessionStore,
    profiles_root: Path,
    admin_secret: str,
    builds: BuildJobManager,
    boot_dir: Optional[Path] = None,
) -> APIRouter:
    """Construct the dashboard router and its template environment."""

    if not TEMPLATES_DIR.is_dir():
        raise RuntimeError(
            f"dashboard templates dir missing: {TEMPLATES_DIR}"
        )

    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    require_admin = _require_admin(admin_secret)

    router = APIRouter()

    # ---- Landing / machines list ----------------------------------------

    @router.get("/", response_class=HTMLResponse, include_in_schema=False)
    def _root() -> RedirectResponse:
        return RedirectResponse(
            url="/dashboard", status_code=status.HTTP_302_FOUND
        )

    @router.get(
        "/dashboard/api/events-snapshot",
        dependencies=[Depends(require_admin)],
    )
    def events_snapshot(
        mac: Optional[str] = None, limit: int = 200,
    ) -> dict:
        """JSON snapshot for the /dashboard/events live view."""
        limit = max(1, min(int(limit), 1000))
        events = registry.recent_boot_events(limit=limit, mac=mac)
        return {
            "events": [
                {
                    "occurred_at": e.occurred_at,
                    "mac": e.mac,
                    "state": e.state,
                    "detail": e.detail or "",
                }
                for e in events
            ],
            "limit": limit,
            "mac_filter": mac,
        }

    @router.get(
        "/dashboard/api/builds-snapshot",
        dependencies=[Depends(require_admin)],
    )
    def builds_snapshot() -> dict:
        """JSON snapshot for the /dashboard/builds live view.

        Live-view viewers care about: which builds are still running,
        their state and elapsed time, and the artifact metadata once
        they complete."""
        jobs = builds.list_jobs()
        return {
            "running": builds.is_running(),
            "jobs": [
                {
                    "job_id": j.job_id,
                    "profile": j.profile,
                    "architecture": j.architecture,
                    "state": j.state.value,
                    "exit_code": j.exit_code,
                    "started_at": j.started_at,
                    "finished_at": j.finished_at,
                    "artifact": (
                        _artifact_for(boot_dir, j.profile, j.architecture)
                        if j.state.value == "succeeded" else None
                    ),
                }
                for j in jobs
            ],
        }

    @router.get(
        "/dashboard/api/machine-snapshot/{mac}",
        dependencies=[Depends(require_admin)],
    )
    def machine_snapshot(mac: str) -> dict:
        """JSON snapshot for the /dashboard/machines/{mac} live view."""
        machine = registry.lookup(mac)
        if machine is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="machine not found",
            )
        states_by_mac = _states_by_mac(sessions)
        last_seen = _format_last_seen(sessions.last_seen_by_mac())
        events = registry.recent_boot_events(limit=30, mac=mac)
        hardware = None
        if machine.last_hardware:
            import json as _json
            try:
                hardware = _json.loads(machine.last_hardware)
            except _json.JSONDecodeError:
                hardware = None
        return {
            "hostname": machine.hostname,
            "last_ip": machine.last_ip,
            "boot_version": machine.boot_version,
            "boot_version_seen_at": machine.boot_version_seen_at,
            "pending_reboot": bool(machine.pending_reboot),
            "current_state": states_by_mac.get(machine.mac),
            "last_seen": last_seen.get(machine.mac),
            "last_diagnostics": machine.last_diagnostics,
            "last_diagnostics_at": machine.last_diagnostics_at,
            "events": [
                {
                    "occurred_at": e.occurred_at,
                    "state": e.state,
                    "detail": e.detail or "",
                }
                for e in events
            ],
            "hardware": hardware,
        }

    @router.get(
        "/dashboard/api/machines-snapshot",
        dependencies=[Depends(require_admin)],
    )
    def machines_snapshot() -> dict:
        """JSON snapshot of every field the machines-list live-view
        polls for. The template's inline JS calls this every few
        seconds and patches individual cells — no full-page reload,
        no meta-refresh.

        Returning only what the live view needs (state, last-seen,
        boot-version comparison, pending-reboot) keeps the payload
        under a few KB even for a fleet of hundreds.
        """
        machines = registry.list_all()
        states_by_mac = _states_by_mac(sessions)
        latest_versions = _latest_versions_by_artefact(boot_dir)
        last_seen = _format_last_seen(sessions.last_seen_by_mac())
        return {
            "machines": [
                {
                    "mac": m.mac,
                    "hostname": m.hostname,
                    "last_ip": m.last_ip,
                    "state": states_by_mac.get(m.mac),
                    "boot_version": m.boot_version,
                    "latest_version": latest_versions.get(
                        f"{m.profile_name}/{m.architecture}"
                    ),
                    "last_seen": last_seen.get(m.mac),
                    "pending_reboot": bool(m.pending_reboot),
                }
                for m in machines
            ],
        }

    @router.get(
        "/dashboard",
        response_class=HTMLResponse,
        dependencies=[Depends(require_admin)],
    )
    def dashboard_home(request: Request) -> HTMLResponse:
        machines = registry.list_all()
        states_by_mac = _states_by_mac(sessions)
        latest_versions = _latest_versions_by_artefact(boot_dir)
        last_seen = _format_last_seen(sessions.last_seen_by_mac())
        return templates.TemplateResponse(
            request,
            "machines.html",
            {
                "machines": machines,
                "states_by_mac": states_by_mac,
                "profile_names": _list_profile_names(profiles_root),
                # Every dashboard page polls a JSON snapshot at this
                # cadence — see startLiveView in base.html.
                "polling_seconds": LIVE_POLL_SECONDS,
                "latest_versions": latest_versions,
                "last_seen": last_seen,
                # Used by the per-row delete+reboot button: if set, any
                # machine with a hostname can be power-cycled via the
                # pdudaemon alias fallback even without its own
                # reboot_command.
                "pdudaemon_host": registry.get_setting("pdudaemon_host"),
            },
        )

    @router.post(
        "/dashboard/machines",
        response_class=HTMLResponse,
        dependencies=[Depends(require_admin)],
    )
    def enrol_machine(
        mac: str = Form(...),
        profile_name: str = Form(...),
        architecture: str = Form("x86_64"),
        platform: str = Form("efi"),
        serial_console: Optional[str] = Form(None),
        scratch_mode: str = Form("volatile"),
    ) -> RedirectResponse:
        if scratch_mode not in ("volatile", "persistent", "off"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="scratch_mode must be volatile/persistent/off",
            )
        registry.enroll(
            mac=mac,
            profile_name=profile_name,
            architecture=architecture,
            platform=platform,
            serial_console=bool(serial_console),
            scratch_mode=scratch_mode,
        )
        return RedirectResponse(
            url="/dashboard", status_code=status.HTTP_303_SEE_OTHER
        )

    @router.post(
        "/dashboard/machines/{mac}/delete",
        response_class=HTMLResponse,
        dependencies=[Depends(require_admin)],
    )
    def delete_machine(mac: str) -> RedirectResponse:
        registry.remove(mac)
        return RedirectResponse(
            url="/dashboard", status_code=status.HTTP_303_SEE_OTHER
        )

    # ---- Settings -------------------------------------------------------

    @router.get(
        "/dashboard/settings",
        response_class=HTMLResponse,
        dependencies=[Depends(require_admin)],
    )
    def show_settings(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "settings.html",
            {
                "pdudaemon_host": registry.get_setting("pdudaemon_host") or "",
            },
        )

    @router.post(
        "/dashboard/settings",
        response_class=HTMLResponse,
        dependencies=[Depends(require_admin)],
    )
    def update_settings(
        pdudaemon_host: str = Form(""),
    ) -> RedirectResponse:
        registry.set_setting("pdudaemon_host", pdudaemon_host)
        return RedirectResponse(
            url="/dashboard/settings",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    # ---- Profiles -------------------------------------------------------

    @router.get(
        "/dashboard/profiles",
        response_class=HTMLResponse,
        dependencies=[Depends(require_admin)],
    )
    def list_profiles(request: Request) -> HTMLResponse:
        names = _list_profile_names(profiles_root)
        # Most-recent artifact per profile. We only check x86_64-shaped
        # archs the image recipe produces today (amd64, arm64); the
        # template hides rows with no artifact yet.
        latest_artifacts: dict[str, dict] = {}
        for name in names:
            for arch in ("amd64", "arm64"):
                info = _artifact_for(boot_dir, name, arch)
                if info is not None:
                    info["architecture"] = arch
                    latest_artifacts[name] = info
                    break
        return templates.TemplateResponse(
            request,
            "profiles.html",
            {
                "profile_names": names,
                "latest_artifacts": latest_artifacts,
            },
        )

    @router.get(
        "/dashboard/profiles/{name}",
        response_class=HTMLResponse,
        dependencies=[Depends(require_admin)],
    )
    def edit_profile(request: Request, name: str) -> HTMLResponse:
        profile_dir = _safe_profile_dir(profiles_root, name)
        if not profile_dir.is_dir():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="profile not found",
            )
        return templates.TemplateResponse(
            request,
            "profile_edit.html",
            {
                "name": name,
                "readme": _read_or_empty(profile_dir / "README.md"),
                "extra_packages": _read_or_empty(
                    profile_dir / "extra-packages.list"
                ),
                "setup_chroot": _read_or_empty(
                    profile_dir / "setup-chroot"
                ),
                # Inheritance chain — newline-separated parent profile
                # names. Each must resolve under profiles_root; the
                # resolver dedupes and unions.
                "parents": _read_or_empty(profile_dir / "parent"),
                # Debian release this profile bases its image on.
                # Default ("trixie") is empty here; the recipe falls
                # back when this file is missing.
                "suite": _read_or_empty(profile_dir / "suite").strip(),
                # Show what's actually available to inherit from.
                "available_parents": [
                    p for p in _list_profile_names(profiles_root) if p != name
                ],
                # Every file that lands in the image via the overlay
                # tree. Each entry has {"path", "size", "is_text"} —
                # is_text drives whether the file gets an editable
                # textarea or a "binary, download only" link.
                "overlay_files": _list_overlay_files(profile_dir),
            },
        )

    @router.get(
        "/dashboard/profiles/{name}/overlay/{relpath:path}",
        response_class=HTMLResponse,
        dependencies=[Depends(require_admin)],
    )
    def edit_overlay_file(
        request: Request, name: str, relpath: str,
    ) -> HTMLResponse:
        profile_dir = _safe_profile_dir(profiles_root, name)
        file_path = _safe_overlay_path(profile_dir, relpath)
        if not file_path.is_file():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="overlay file not found",
            )
        try:
            content = file_path.read_text()
            is_text = True
        except UnicodeDecodeError:
            content = ""
            is_text = False
        return templates.TemplateResponse(
            request,
            "profile_overlay_edit.html",
            {
                "profile_name": name,
                "relpath": relpath,
                "content": content,
                "is_text": is_text,
                "size": file_path.stat().st_size,
            },
        )

    # NB: the delete route must be declared BEFORE the generic
    # save route so FastAPI's greedy `{relpath:path}` matcher on save
    # doesn't grab `/delete` as part of the relpath.
    @router.post(
        "/dashboard/profiles/{name}/overlay/{relpath:path}/delete",
        response_class=HTMLResponse,
        dependencies=[Depends(require_admin)],
    )
    def delete_overlay_file(name: str, relpath: str) -> RedirectResponse:
        profile_dir = _safe_profile_dir(profiles_root, name)
        file_path = _safe_overlay_path(profile_dir, relpath)
        if file_path.is_file():
            file_path.unlink()
            # Clean up empty parent dirs up to overlay/ root.
            overlay_root = (profile_dir / "overlay").resolve()
            parent = file_path.parent
            while parent != overlay_root and not any(parent.iterdir()):
                parent.rmdir()
                parent = parent.parent
        return RedirectResponse(
            url=f"/dashboard/profiles/{name}",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    @router.post(
        "/dashboard/profiles/{name}/overlay",
        response_class=HTMLResponse,
        dependencies=[Depends(require_admin)],
    )
    def create_overlay_file(
        name: str, relpath: str = Form(...), content: str = Form(""),
    ) -> RedirectResponse:
        profile_dir = _safe_profile_dir(profiles_root, name)
        # Don't strip a leading slash — pass the value as-is to
        # _safe_overlay_path so that path traversal (`/etc/passwd`)
        # is caught there and rejected with 400.
        cleaned = relpath.strip()
        if not cleaned:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="relpath is required",
            )
        file_path = _safe_overlay_path(profile_dir, cleaned)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content)
        return RedirectResponse(
            url=f"/dashboard/profiles/{name}",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    @router.post(
        "/dashboard/profiles/{name}/overlay/{relpath:path}",
        response_class=HTMLResponse,
        dependencies=[Depends(require_admin)],
    )
    def save_overlay_file(
        name: str, relpath: str, content: str = Form(""),
    ) -> RedirectResponse:
        profile_dir = _safe_profile_dir(profiles_root, name)
        file_path = _safe_overlay_path(profile_dir, relpath)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content)
        return RedirectResponse(
            url=f"/dashboard/profiles/{name}",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    @router.post(
        "/dashboard/profiles/{name}",
        response_class=HTMLResponse,
        dependencies=[Depends(require_admin)],
    )
    def save_profile(
        name: str,
        extra_packages: str = Form(""),
        setup_chroot: str = Form(""),
        readme: str = Form(""),
        parents: str = Form(""),
        suite: str = Form(""),
    ) -> RedirectResponse:
        profile_dir = _safe_profile_dir(profiles_root, name)
        profile_dir.mkdir(parents=True, exist_ok=True)
        (profile_dir / "extra-packages.list").write_text(extra_packages)
        (profile_dir / "README.md").write_text(readme)
        # setup-chroot is only written (and made executable) if non-empty.
        # Empty means "no setup-chroot for this profile" and we delete
        # whatever was there.
        script_path = profile_dir / "setup-chroot"
        if setup_chroot.strip():
            script_path.write_text(setup_chroot)
            script_path.chmod(0o755)
        elif script_path.is_file():
            script_path.unlink()
        # `parent` is newline-separated names; empty clears the file
        # entirely so the resolver treats this profile as a root.
        # Sanitise to known profile names — silently drop unknown
        # entries so a typo can't break the build chain. (The
        # resolver also validates, but we surface it earlier here.)
        known = set(_list_profile_names(profiles_root)) - {name}
        cleaned_parents = "\n".join(
            line.strip() for line in parents.splitlines()
            if line.strip() and line.strip() in known
        )
        parent_path = profile_dir / "parent"
        if cleaned_parents:
            parent_path.write_text(cleaned_parents + "\n")
        elif parent_path.is_file():
            parent_path.unlink()
        # `suite` is the Debian release codename (e.g. "trixie",
        # "bookworm"). Empty clears the per-profile override so the
        # recipe falls back to its default.
        suite_path = profile_dir / "suite"
        cleaned_suite = suite.strip()
        if cleaned_suite:
            suite_path.write_text(cleaned_suite + "\n")
        elif suite_path.is_file():
            suite_path.unlink()
        return RedirectResponse(
            url=f"/dashboard/profiles/{name}",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    # ---- Builds ---------------------------------------------------------

    @router.get(
        "/dashboard/auto-enrol-rules",
        response_class=HTMLResponse,
        dependencies=[Depends(require_admin)],
    )
    def list_auto_enrol_rules_page(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "auto_enrol_rules.html",
            {
                "rules": registry.list_auto_enrol_rules(),
                "profile_names": _list_profile_names(profiles_root),
            },
        )

    @router.post(
        "/dashboard/auto-enrol-rules",
        response_class=HTMLResponse,
        dependencies=[Depends(require_admin)],
    )
    def add_auto_enrol_rule_form(
        name: str = Form(...),
        match_kind: str = Form(...),
        match_value: str = Form(""),
        profile_name: str = Form(...),
        architecture: str = Form("x86_64"),
        platform: str = Form("any"),
        serial_console: Optional[str] = Form(None),
        scratch_mode: str = Form("volatile"),
    ) -> RedirectResponse:
        if match_kind not in ("mac_prefix", "ip_cidr"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="invalid match_kind",
            )
        if platform not in ("any", "efi", "pc"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="platform must be 'any', 'efi', or 'pc'",
            )
        if scratch_mode not in ("volatile", "persistent", "off"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="scratch_mode must be volatile/persistent/off",
            )
        try:
            registry.add_auto_enrol_rule(
                name=name,
                match_kind=match_kind,
                match_value=match_value,
                profile_name=profile_name,
                architecture=architecture,
                platform=platform,
                serial_console=bool(serial_console),
                scratch_mode=scratch_mode,
            )
        except ValueError as err:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(err),
            )
        return RedirectResponse(
            url="/dashboard/auto-enrol-rules",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    @router.post(
        "/dashboard/auto-enrol-rules/{rule_id}",
        response_class=HTMLResponse,
        dependencies=[Depends(require_admin)],
    )
    def update_auto_enrol_rule_form(
        rule_id: int,
        name: str = Form(...),
        match_kind: str = Form(...),
        match_value: str = Form(""),
        profile_name: str = Form(...),
        architecture: str = Form("x86_64"),
        platform: str = Form("any"),
        serial_console: Optional[str] = Form(None),
        scratch_mode: str = Form("volatile"),
    ) -> RedirectResponse:
        if match_kind not in ("mac_prefix", "ip_cidr"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="invalid match_kind",
            )
        if platform not in ("any", "efi", "pc"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="platform must be 'any', 'efi', or 'pc'",
            )
        if scratch_mode not in ("volatile", "persistent", "off"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="scratch_mode must be volatile/persistent/off",
            )
        try:
            updated = registry.update_auto_enrol_rule(
                rule_id,
                name=name,
                match_kind=match_kind,
                match_value=match_value,
                profile_name=profile_name,
                architecture=architecture,
                platform=platform,
                serial_console=bool(serial_console),
                scratch_mode=scratch_mode,
            )
        except ValueError as err:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(err),
            )
        if updated is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="rule not found",
            )
        return RedirectResponse(
            url="/dashboard/auto-enrol-rules",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    @router.post(
        "/dashboard/auto-enrol-rules/{rule_id}/delete",
        response_class=HTMLResponse,
        dependencies=[Depends(require_admin)],
    )
    def delete_auto_enrol_rule_form(rule_id: int) -> RedirectResponse:
        registry.remove_auto_enrol_rule(rule_id)
        return RedirectResponse(
            url="/dashboard/auto-enrol-rules",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    @router.post(
        "/dashboard/machines/{mac}/reboot-command",
        response_class=HTMLResponse,
        dependencies=[Depends(require_admin)],
    )
    def set_reboot_command(
        mac: str,
        reboot_command: str = Form(""),
    ) -> RedirectResponse:
        registry.set_reboot_command(mac, reboot_command)
        return RedirectResponse(
            url=f"/dashboard/machines/{mac}",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    @router.post(
        "/dashboard/machines/{mac}/delete-and-reboot",
        response_class=HTMLResponse,
        dependencies=[Depends(require_admin)],
    )
    def delete_and_reboot(mac: str) -> RedirectResponse:
        """Reboot the host, then delete its row if PDU accepted."""
        _reboot_machine(mac, then_delete=True)
        return RedirectResponse(
            url="/dashboard", status_code=status.HTTP_303_SEE_OTHER,
        )

    @router.post(
        "/dashboard/machines/{mac}/reboot",
        response_class=HTMLResponse,
        dependencies=[Depends(require_admin)],
    )
    def reboot_machine_route(mac: str) -> RedirectResponse:
        """Reboot but keep the row. Useful when the admin wants the
        machine power-cycled (or soft-rebooted via the in-image
        reporter) without re-triggering auto-enrolment."""
        _reboot_machine(mac, then_delete=False)
        return RedirectResponse(
            url="/dashboard", status_code=status.HTTP_303_SEE_OTHER,
        )

    def _reboot_machine(mac: str, *, then_delete: bool) -> None:
        """Shared reboot path used by /reboot and /delete-and-reboot.

        Two PDU paths in priority order:
          1. Explicit per-machine `reboot_command` (free-text shell run
             with shell=True on the fleetboot host — admin owns the
             contents).
          2. Fleet-wide pdudaemon fallback when `pdudaemon_host` is set
             AND the machine has a known hostname: builds
             `curl --fail "<pdudaemon>/...?alias=<hostname>"`.

        Behaviour split by `then_delete`:

          then_delete=True (the "del + reboot" button):
            Always delete the row. Try PDU. The soft-reboot signal is
            NOT armed here because /status can't deliver
            `pending_reboot: true` for a deleted row — the boot
            session lookup would find the token but the machine row
            would be gone, so machine.pending_reboot can't be read.
            If PDU fails, the row is gone but the machine keeps
            running until its next manual reboot. That's the
            trade-off the admin opts into by hitting "delete".

          then_delete=False (the standalone "reboot" button):
            Never delete. Try PDU. If PDU fails or isn't configured,
            arm the soft-reboot signal so the machine reboots itself
            on its next heartbeat. The row stays so /status can ride
            the signal out.
        """
        import subprocess

        machine = registry.lookup(mac)
        command: Optional[str] = None
        if machine is not None:
            if machine.reboot_command:
                command = machine.reboot_command
            else:
                pdu_host = registry.get_setting("pdudaemon_host")
                if pdu_host and machine.hostname:
                    command = _pdudaemon_reboot_command(
                        pdu_host=pdu_host, alias=machine.hostname,
                    )

        pdu_succeeded = False
        if command:
            try:
                # 10-second timeout keeps the dashboard responsive
                # when the PDU host is unreachable. shell=True is
                # intentional — admin owns the command string.
                result = subprocess.run(
                    command, shell=True, timeout=10,
                    stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
                )
                pdu_succeeded = result.returncode == 0
            except subprocess.TimeoutExpired:
                pdu_succeeded = False

        if then_delete:
            # Admin asked for the row to go; honour that even if PDU
            # failed. (See docstring for why we can't soft-reboot a
            # deleted row.)
            registry.remove(mac)
        elif not pdu_succeeded:
            # /reboot button, PDU failed/unset — arm the soft signal.
            registry.set_pending_reboot(mac, True)

    @router.get(
        "/dashboard/machines/{mac}",
        response_class=HTMLResponse,
        dependencies=[Depends(require_admin)],
    )
    def machine_detail(
        request: Request,
        mac: str,
    ) -> HTMLResponse:
        machine = registry.lookup(mac)
        if machine is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="machine not found",
            )
        states_by_mac = _states_by_mac(sessions)
        latest_versions = _latest_versions_by_artefact(boot_dir)
        events = registry.recent_boot_events(limit=200, mac=machine.mac)
        last_seen = _format_last_seen(sessions.last_seen_by_mac())
        hardware: Optional[dict] = None
        if machine.last_hardware:
            import json as _json
            try:
                hardware = _json.loads(machine.last_hardware)
            except _json.JSONDecodeError:
                hardware = None
        return templates.TemplateResponse(
            request,
            "machine_detail.html",
            {
                "machine": machine,
                "events": events,
                "current_state": states_by_mac.get(machine.mac),
                "latest_versions": latest_versions,
                "last_seen": last_seen.get(machine.mac),
                "polling_seconds": LIVE_POLL_SECONDS,
                "hardware": hardware,
            },
        )

    @router.get(
        "/dashboard/events",
        response_class=HTMLResponse,
        dependencies=[Depends(require_admin)],
    )
    def list_events(
        request: Request,
        mac: Optional[str] = None,
        limit: int = 200,
    ) -> HTMLResponse:
        # Clamp `limit` so a bogus query string can't trigger a huge fetch.
        limit = max(1, min(int(limit), 1000))
        events = registry.recent_boot_events(limit=limit, mac=mac)
        return templates.TemplateResponse(
            request,
            "events.html",
            {
                "events": events,
                "mac_filter": mac,
                "limit": limit,
                "polling_seconds": LIVE_POLL_SECONDS,
            },
        )

    @router.get(
        "/dashboard/builds",
        response_class=HTMLResponse,
        dependencies=[Depends(require_admin)],
    )
    def list_builds(
        request: Request,
        error: Optional[str] = None,
    ) -> HTMLResponse:
        jobs = builds.list_jobs()
        # Successful builds: surface the artifact filename + size on
        # disk so admins can confirm at-a-glance what landed. Failed /
        # running builds get None — the template suppresses the cell.
        artifacts = {
            j.job_id: _artifact_for(boot_dir, j.profile, j.architecture)
            if getattr(j, "state", "") == "succeeded" else None
            for j in jobs
        }
        return templates.TemplateResponse(
            request,
            "builds.html",
            {
                "jobs": jobs,
                "running": builds.is_running(),
                "profile_names": _list_profile_names(profiles_root),
                "artifacts": artifacts,
                "error": error,
                "polling_seconds": LIVE_POLL_SECONDS,
            },
        )

    @router.post(
        "/dashboard/builds",
        response_class=HTMLResponse,
        dependencies=[Depends(require_admin)],
    )
    def trigger_build(
        profile: str = Form(...),
        architecture: str = Form("amd64"),
    ) -> RedirectResponse:
        # Validate profile name and that it actually exists.
        _safe_profile_dir(profiles_root, profile)
        if not (profiles_root / profile).is_dir():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="profile not found",
            )
        try:
            job = builds.start(profile=profile, architecture=architecture)
        except BuildAlreadyRunningError:
            # Don't crash; surface as a flash on the builds page.
            return RedirectResponse(
                url="/dashboard/builds?error=already-running",
                status_code=status.HTTP_303_SEE_OTHER,
            )
        return RedirectResponse(
            url=f"/dashboard/builds/{job.job_id}",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    @router.get(
        "/dashboard/builds/{job_id}",
        response_class=HTMLResponse,
        dependencies=[Depends(require_admin)],
    )
    def view_build(
        request: Request,
        job_id: str,
        follow: Optional[int] = None,
    ) -> HTMLResponse:
        job = builds.get(job_id)
        if job is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="build not found",
            )
        # The build_detail template already auto-reloads while the build
        # is in flight; `follow` additionally scrolls the log <pre> to the
        # bottom each load so newest lines stay in view.
        return templates.TemplateResponse(
            request,
            "build_detail.html",
            {
                "job": job,
                "log_lines": builds.tail_log(job_id, n=400),
                "follow": bool(follow),
            },
        )

    return router


# ---- Helpers -------------------------------------------------------------


def _safe_overlay_path(profile_dir: Path, relpath: str) -> Path:
    """Resolve `relpath` under `profile_dir/overlay/` and refuse anything
    outside that root. Guards against `..`-based traversal AND
    absolute paths smuggled in via the URL segment. `relpath` may be
    multi-segment (`etc/systemd/system/foo.service`).
    """
    # Reject up-front:
    #   - absolute paths (which `Path(...) / '/etc/passwd'` would happily
    #     collapse to just '/etc/passwd', escaping our overlay root)
    #   - any segment that is `..`, `.`, or empty (double-slash tricks)
    if not relpath or relpath.startswith("/"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="invalid overlay path",
        )
    for part in Path(relpath).parts:
        if part in ("..", "", ".", "/"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="invalid overlay path",
            )
    overlay_root = (profile_dir / "overlay").resolve()
    candidate = (overlay_root / relpath).resolve()
    try:
        candidate.relative_to(overlay_root)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="invalid overlay path",
        )
    return candidate


def _list_overlay_files(profile_dir: Path) -> list[dict]:
    """List every file under profile_dir/overlay/ with metadata for
    the profile-edit template. Returns entries sorted by relative
    path so the UI is stable across saves."""
    overlay_root = profile_dir / "overlay"
    if not overlay_root.is_dir():
        return []
    entries: list[dict] = []
    for path in sorted(overlay_root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(overlay_root).as_posix()
        # Rough heuristic: try to decode a bit as UTF-8. If it works
        # we treat it as editable text; otherwise the UI shows it as
        # a binary blob with delete-only affordance.
        try:
            with path.open("rb") as f:
                head = f.read(4096)
            head.decode("utf-8")
            is_text = True
        except UnicodeDecodeError:
            is_text = False
        entries.append({
            "path": rel, "size": path.stat().st_size, "is_text": is_text,
        })
    return entries


def _pdudaemon_reboot_command(*, pdu_host: str, alias: str) -> str:
    """Build the curl line that asks pdudaemon to reboot a given alias.

    The hostname is URL-quoted because aliases can legitimately include
    characters that need escaping (host naming schemes often use dots or
    dashes). pdu_host is taken verbatim — the admin set it so they can
    include port, scheme, etc.
    """
    from urllib.parse import quote
    safe_alias = quote(alias, safe="")
    # pdu_host typically lacks the scheme; default to http if missing
    # so the admin can just paste "prowl:16421".
    if "://" in pdu_host:
        base = pdu_host
    else:
        base = f"http://{pdu_host}"
    # `--fail` makes curl exit non-zero on HTTP 4xx/5xx so our
    # soft-reboot fallback fires when pdudaemon refuses the request
    # (unknown alias, port unconfigured, server down). Without it
    # curl returns 0 for a "404 alias not found" body, which would
    # masquerade as success.
    return f'curl --fail "{base}/power/control/reboot?alias={safe_alias}"'


def _list_profile_names(profiles_root: Path) -> list[str]:
    """Return profile directory names, sorted, excluding the README index."""
    if not profiles_root.is_dir():
        return []
    return sorted(
        p.name for p in profiles_root.iterdir() if p.is_dir()
    )


def _states_by_mac(sessions: BootSessionStore) -> dict[str, str]:
    """Latest-known boot state per MAC, from active sessions."""
    out: dict[str, str] = {}
    for session in sessions.active_sessions():
        if session.latest_state is not None:
            out[session.mac] = session.latest_state.value
    return out


# Stale threshold: the in-image heartbeat fires every 2 min, so anything
# older than three intervals is "really gone, not just a missed tick".
_STALE_AFTER_SECONDS = 6 * 60


def _format_last_seen(
    raw: dict[str, str], now=None,
) -> dict[str, dict[str, object]]:
    """Turn the {mac: ISO timestamp} map from BootSessionStore into a
    template-friendly {mac: {"label": "5m ago", "stale": False}}."""
    from datetime import datetime, timezone

    current = now if now is not None else datetime.now(timezone.utc)
    out: dict[str, dict[str, object]] = {}
    for mac, raw_ts in raw.items():
        try:
            # SQLite's datetime('now') returns "YYYY-MM-DD HH:MM:SS" in UTC.
            parsed = datetime.fromisoformat(raw_ts).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        delta = current - parsed
        seconds = max(0, int(delta.total_seconds()))
        out[mac] = {
            "label": _humanise_seconds(seconds),
            "stale": seconds >= _STALE_AFTER_SECONDS,
        }
    return out


def _humanise_seconds(seconds: int) -> str:
    """Compact relative-time formatter — 's', 'm', 'h', 'd'."""
    if seconds < 60:
        return f"{seconds}s ago"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 48:
        return f"{hours}h ago"
    days = hours // 24
    return f"{days}d ago"


def _latest_versions_by_artefact(boot_dir: Optional[Path]) -> dict[str, str]:
    """Scan boot_dir for fleetboot-<profile>-<arch>.version sidecars.

    Returns a {"<profile>/<arch>": "<version>"} map. The machines
    template looks up its own row's profile+arch and compares.

    The same version is registered under every alias for that arch
    (e.g. an `amd64` sidecar is keyed as both `<profile>/amd64` and
    `<profile>/x86_64`) because the registry stores `x86_64` for
    BIOS PXE clients while build artefacts always use Debian's
    `amd64`. Without the alias the dashboard would render every
    BIOS-PXE'd machine's version in grey.
    """
    # Map every architecture name to its set of aliases. Lookups
    # apply on BOTH the row's arch (whatever the registry stored)
    # AND every sidecar's arch.
    arch_aliases = {
        "amd64": ("amd64", "x86_64", "x64"),
        "x86_64": ("amd64", "x86_64", "x64"),
        "x64": ("amd64", "x86_64", "x64"),
        "arm64": ("arm64", "aarch64"),
        "aarch64": ("arm64", "aarch64"),
    }
    if boot_dir is None or not boot_dir.is_dir():
        return {}
    out: dict[str, str] = {}
    for path in boot_dir.glob("fleetboot-*-*.version"):
        # Filename shape: fleetboot-<profile>-<arch>.version
        stem = path.stem
        parts = stem.split("-")
        if len(parts) < 3 or parts[0] != "fleetboot":
            continue
        arch = parts[-1]
        # Profile may itself contain a hyphen, so reassemble the middle.
        profile = "-".join(parts[1:-1])
        try:
            version = path.read_text().strip().splitlines()[0]
        except (OSError, IndexError):
            continue
        if not version:
            continue
        for alias in arch_aliases.get(arch, (arch,)):
            out[f"{profile}/{alias}"] = version
    return out


def _artifact_for(
    boot_dir: Optional[Path], profile: str, architecture: str,
) -> Optional[dict]:
    """Return metadata for the squashfs that `make image PROFILE=...
    ARCH=...` produces, or None if it doesn't exist on disk.

    Used by the builds and profiles pages so admins can see at a glance
    "what file was created, how big is it, when was it written". We
    look at the artifact's *current* on-disk state — successive builds
    of the same (profile, arch) overwrite each other, so this naturally
    shows the most recent build for that combination.
    """
    if boot_dir is None:
        return None
    name = f"fleetboot-{profile}-{architecture}.squashfs"
    path = boot_dir / name
    try:
        st = path.stat()
    except OSError:
        return None
    return {
        "name": name,
        "size_mb": st.st_size // (1024 * 1024),
        "mtime": _format_mtime(st.st_mtime),
    }


def _format_mtime(epoch: float) -> str:
    """Human-readable timestamp for an artifact mtime."""
    import datetime
    return datetime.datetime.fromtimestamp(
        epoch, tz=datetime.timezone.utc,
    ).strftime("%Y-%m-%d %H:%M UTC")


# Every live-view page polls its snapshot endpoint at this cadence.
# 5s is fast enough to feel live for lifecycle-state changes and
# heartbeats, slow enough that fleet size stays comfortable server-
# side. Not a per-page setting: an admin who wants a different
# cadence overrides at browser level (open dev tools, adjust the
# interval) rather than us adding UI for it.
LIVE_POLL_SECONDS = 5


def _read_or_empty(path: Path) -> str:
    try:
        return path.read_text()
    except FileNotFoundError:
        return ""
