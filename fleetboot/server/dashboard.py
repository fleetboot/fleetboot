"""Operational dashboard.

Tiny FastAPI + Jinja2 HTML UI for an administrator to see registered
machines, edit profiles, and trigger image builds.

Auth: HTTP Basic — the admin secret is the password, any username works.
This is intended for use inside a school's admin network; for public
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
        "/dashboard",
        response_class=HTMLResponse,
        dependencies=[Depends(require_admin)],
    )
    def dashboard_home(
        request: Request, refresh: Optional[int] = None,
    ) -> HTMLResponse:
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
                "auto_refresh": _clamp_refresh(refresh),
                "latest_versions": latest_versions,
                "last_seen": last_seen,
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

    # ---- Profiles -------------------------------------------------------

    @router.get(
        "/dashboard/profiles",
        response_class=HTMLResponse,
        dependencies=[Depends(require_admin)],
    )
    def list_profiles(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "profiles.html",
            {"profile_names": _list_profile_names(profiles_root)},
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
            },
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

    @router.get(
        "/dashboard/events",
        response_class=HTMLResponse,
        dependencies=[Depends(require_admin)],
    )
    def list_events(
        request: Request,
        mac: Optional[str] = None,
        limit: int = 200,
        refresh: Optional[int] = None,
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
                "auto_refresh": _clamp_refresh(refresh),
            },
        )

    @router.get(
        "/dashboard/builds",
        response_class=HTMLResponse,
        dependencies=[Depends(require_admin)],
    )
    def list_builds(
        request: Request, error: Optional[str] = None,
    ) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "builds.html",
            {
                "jobs": builds.list_jobs(),
                "running": builds.is_running(),
                "profile_names": _list_profile_names(profiles_root),
                "error": error,
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

    Returns a {"<profile>/<arch>": "<version>"} map. The machines template
    looks up its own row's profile+arch and compares.
    """
    if boot_dir is None or not boot_dir.is_dir():
        return {}
    out: dict[str, str] = {}
    for path in boot_dir.glob("fleetboot-*-*.version"):
        # Filename shape: fleetboot-<profile>-<arch>.version
        stem = path.stem  # "fleetboot-<profile>-<arch>"
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
        if version:
            out[f"{profile}/{arch}"] = version
    return out


def _clamp_refresh(value: Optional[int]) -> Optional[int]:
    """Constrain the ?refresh= query param to a sensible range.

    Returns None if the query param wasn't present (no meta tag emitted),
    otherwise an integer in [2, 60] — fast enough to feel live, slow
    enough not to hammer the server.
    """
    if value is None:
        return None
    try:
        return max(2, min(int(value), 60))
    except (TypeError, ValueError):
        return None


def _read_or_empty(path: Path) -> str:
    try:
        return path.read_text()
    except FileNotFoundError:
        return ""
