"""MCP (Model Context Protocol) endpoint.

Exposes fleetboot's control-plane operations as MCP tools so an LLM
agent can drive it — list/enrol/reboot machines, author profiles,
kick off builds, watch events.

Transport: Streamable HTTP in its simplest form — POST /mcp with a
JSON-RPC 2.0 body, response is a single JSON body (no SSE stream).
Notifications get a 202 No Body. Batch requests are supported.

Auth: `Authorization: Bearer <admin_secret>` — the same secret that
gates the dashboard. MCP over HTTP is a Bearer-token protocol in
practice; matching it here keeps agent config simple.
"""

from __future__ import annotations

import hmac
import json
from pathlib import Path
from typing import Any, Callable, Optional

from fastapi import APIRouter, Header, HTTPException, Request, Response, status

from fleetboot.server.build_jobs import (
    BuildAlreadyRunningError,
    BuildJobManager,
)
from fleetboot.server.dashboard import (
    _list_profile_names,
    _safe_overlay_path,
    _safe_profile_dir,
    reboot_machine,
)
from fleetboot.server.registry import MachineRegistry


PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {"name": "fleetboot", "version": "0.0.1"}

# Top-level guidance the model receives at initialize time. MCP clients
# feed this into the system prompt so it shapes tool selection before
# any tool is called.
SERVER_INSTRUCTIONS = """\
fleetboot is a netboot fleet control plane for locked-down Debian
machines. Everything a machine boots into is produced by an "image
build" from a named "profile" (a directory under image/profiles/ that
lists extra packages, a setup-chroot script, an overlay/ tree, and an
optional parent chain).

Typical workflows an agent can drive here:

  Add a machine to the fleet
    1. `list_profiles` to see what's on offer.
    2. `enrol_machine` with its MAC + profile_name.
    3. Optionally `set_reboot_command` if this box has its own PDU.

  Create or tweak a profile, then deploy it
    1. `save_profile` (creates or updates the directory).
    2. `write_profile_overlay` for any files that ship inside the image.
    3. `start_build` — returns a job_id.
    4. Poll `get_build` until state is "succeeded".
    5. `reboot_machine` on the affected MACs so they PXE the new image.

  Reboot a machine
    - `reboot_machine` tries the per-machine reboot_command, then the
      fleet pdudaemon fallback; if both are absent or fail, it arms
      the soft-reboot signal so the machine reboots itself on its
      next /status heartbeat.
    - Pass delete_after=true only when you also want the registry row
      gone (a soft-reboot cannot deliver to a deleted row).

  Watch the fleet
    - `list_machines` for a snapshot; `recent_events` for a live feel
      of what states machines are hitting.

State is durable: profile edits land on disk, enrolments land in
SQLite, builds produce artefacts under build/. There is no undo — a
delete or a save overwrites in place.
"""


# ---- JSON-RPC error codes -----------------------------------------------
_PARSE_ERROR = -32700
_INVALID_REQUEST = -32600
_METHOD_NOT_FOUND = -32601
_INVALID_PARAMS = -32602
_INTERNAL_ERROR = -32603


def _rpc_error(id_: Any, code: int, message: str) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": id_,
        "error": {"code": code, "message": message},
    }


def _rpc_result(id_: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": id_, "result": result}


def _tool_text_result(payload: Any, *, is_error: bool = False) -> dict:
    """Wrap a Python object as MCP tool content: one JSON text block."""
    return {
        "content": [
            {"type": "text", "text": json.dumps(payload, default=str)},
        ],
        "isError": is_error,
    }


def build_mcp_router(
    *,
    registry: MachineRegistry,
    profiles_root: Path,
    builds: BuildJobManager,
    admin_secret: str,
) -> APIRouter:
    """Construct the /mcp router."""

    router = APIRouter()

    def check_auth(authorization: Optional[str]) -> None:
        if not authorization or not authorization.lower().startswith(
            "bearer "
        ):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Bearer token required",
                headers={"WWW-Authenticate": "Bearer"},
            )
        token = authorization[len("Bearer "):].strip()
        if not hmac.compare_digest(token, admin_secret):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="unauthorised",
            )

    tools = _tool_catalogue()
    tool_impls = _build_tool_impls(
        registry=registry,
        profiles_root=profiles_root,
        builds=builds,
    )

    @router.get("/mcp", include_in_schema=False)
    def _mcp_get(
        authorization: Optional[str] = Header(default=None),
    ) -> Response:
        """MCP allows GET for the server->client SSE channel; we don't
        support server-initiated messages, so answer with 405."""
        check_auth(authorization)
        return Response(status_code=status.HTTP_405_METHOD_NOT_ALLOWED)

    @router.post("/mcp")
    async def _mcp_post(
        request: Request,
        authorization: Optional[str] = Header(default=None),
    ) -> Response:
        check_auth(authorization)
        try:
            raw = await request.body()
            payload = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            return _json_response(
                _rpc_error(None, _PARSE_ERROR, "invalid JSON"),
            )
        if payload is None:
            return _json_response(
                _rpc_error(None, _INVALID_REQUEST, "empty body"),
            )

        # Batch: array of requests → array of responses (notifications
        # drop out of the response array).
        if isinstance(payload, list):
            out: list[dict] = []
            for entry in payload:
                resp = _dispatch(entry, tools, tool_impls)
                if resp is not None:
                    out.append(resp)
            if not out:
                return Response(status_code=status.HTTP_202_ACCEPTED)
            return _json_response(out)

        resp = _dispatch(payload, tools, tool_impls)
        if resp is None:
            return Response(status_code=status.HTTP_202_ACCEPTED)
        return _json_response(resp)

    return router


def _json_response(body: Any) -> Response:
    return Response(
        content=json.dumps(body),
        media_type="application/json",
    )


def _dispatch(
    req: Any,
    tools: list[dict],
    tool_impls: dict[str, Callable[[dict], Any]],
) -> Optional[dict]:
    """Handle one JSON-RPC request. Returns None for notifications."""
    if not isinstance(req, dict):
        return _rpc_error(None, _INVALID_REQUEST, "not a JSON object")
    id_ = req.get("id")
    is_notification = "id" not in req
    method = req.get("method")
    params = req.get("params") or {}
    if not isinstance(method, str):
        return None if is_notification else _rpc_error(
            id_, _INVALID_REQUEST, "missing method",
        )

    try:
        if method == "initialize":
            result = {
                "protocolVersion": PROTOCOL_VERSION,
                "serverInfo": SERVER_INFO,
                "capabilities": {"tools": {"listChanged": False}},
                "instructions": SERVER_INSTRUCTIONS,
            }
        elif method == "notifications/initialized" or method == "initialized":
            # Notification — no response.
            return None
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": tools}
        elif method == "tools/call":
            name = params.get("name")
            arguments = params.get("arguments") or {}
            if not isinstance(name, str) or name not in tool_impls:
                return None if is_notification else _rpc_error(
                    id_, _METHOD_NOT_FOUND,
                    f"unknown tool: {name!r}",
                )
            if not isinstance(arguments, dict):
                return None if is_notification else _rpc_error(
                    id_, _INVALID_PARAMS,
                    "arguments must be an object",
                )
            try:
                result = tool_impls[name](arguments)
            except _ToolError as err:
                result = _tool_text_result(
                    {"error": str(err)}, is_error=True,
                )
        else:
            return None if is_notification else _rpc_error(
                id_, _METHOD_NOT_FOUND, f"unknown method: {method!r}",
            )
    except Exception as err:  # pragma: no cover — safety net
        return None if is_notification else _rpc_error(
            id_, _INTERNAL_ERROR, str(err),
        )

    if is_notification:
        return None
    return _rpc_result(id_, result)


class _ToolError(Exception):
    """Raised by a tool impl to surface a user-visible error as an MCP
    tool result with isError=true, rather than a JSON-RPC error."""


def _tool_catalogue() -> list[dict]:
    """The tool list advertised via tools/list.

    Kept in one place so the schemas and the impls stay in sync.
    """
    return [
        {
            "name": "list_machines",
            "description": (
                "List every enrolled machine — MAC, hostname, profile, "
                "architecture, last observed IP, boot version, and "
                "whether a soft-reboot signal is armed."
            ),
            "inputSchema": {
                "type": "object", "properties": {}, "additionalProperties": False,
            },
        },
        {
            "name": "get_machine",
            "description": "Return the full record for a single MAC.",
            "inputSchema": {
                "type": "object",
                "properties": {"mac": {"type": "string"}},
                "required": ["mac"],
                "additionalProperties": False,
            },
        },
        {
            "name": "enrol_machine",
            "description": (
                "Register a machine in the fleet. If the MAC is already "
                "enrolled the existing row is updated in place."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "mac": {"type": "string"},
                    "profile_name": {"type": "string"},
                    "architecture": {"type": "string", "default": "x86_64"},
                    "platform": {
                        "type": "string", "enum": ["efi", "pc"],
                        "default": "efi",
                    },
                    "serial_console": {
                        "type": "boolean", "default": False,
                    },
                    "scratch_mode": {
                        "type": "string",
                        "enum": ["volatile", "persistent", "off"],
                        "default": "volatile",
                    },
                },
                "required": ["mac", "profile_name"],
                "additionalProperties": False,
            },
        },
        {
            "name": "delete_machine",
            "description": "Remove a machine's registry row.",
            "inputSchema": {
                "type": "object",
                "properties": {"mac": {"type": "string"}},
                "required": ["mac"],
                "additionalProperties": False,
            },
        },
        {
            "name": "reboot_machine",
            "description": (
                "Reboot a machine. Tries the per-machine reboot_command "
                "or the fleet pdudaemon fallback; if neither succeeds "
                "the soft-reboot signal is armed so the machine reboots "
                "itself on its next /status heartbeat. Set "
                "`delete_after` to remove the registry row afterwards."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "mac": {"type": "string"},
                    "delete_after": {"type": "boolean", "default": False},
                },
                "required": ["mac"],
                "additionalProperties": False,
            },
        },
        {
            "name": "set_pending_reboot",
            "description": (
                "Arm or clear the soft-reboot signal for a machine "
                "without touching PDU. The next /status heartbeat "
                "will carry pending_reboot=true when armed."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "mac": {"type": "string"},
                    "armed": {"type": "boolean"},
                },
                "required": ["mac", "armed"],
                "additionalProperties": False,
            },
        },
        {
            "name": "set_reboot_command",
            "description": (
                "Set (or clear, with empty string) the free-text shell "
                "command that reboots a specific machine. Runs on the "
                "fleetboot host with shell=True."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "mac": {"type": "string"},
                    "command": {"type": "string"},
                },
                "required": ["mac", "command"],
                "additionalProperties": False,
            },
        },
        {
            "name": "list_profiles",
            "description": "Return the names of every profile directory.",
            "inputSchema": {
                "type": "object", "properties": {}, "additionalProperties": False,
            },
        },
        {
            "name": "get_profile",
            "description": (
                "Return a profile's editable content: extra packages, "
                "setup-chroot script, README, parent chain, and Debian "
                "suite override."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
                "additionalProperties": False,
            },
        },
        {
            "name": "save_profile",
            "description": (
                "Create or overwrite a profile. Any omitted field is "
                "left unchanged on an existing profile; on a new "
                "profile the omitted field starts empty. Directory is "
                "created if missing."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "extra_packages": {"type": "string"},
                    "setup_chroot": {"type": "string"},
                    "readme": {"type": "string"},
                    "parents": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Parent profile names, in order.",
                    },
                    "suite": {"type": "string"},
                },
                "required": ["name"],
                "additionalProperties": False,
            },
        },
        {
            "name": "write_profile_overlay",
            "description": (
                "Write a file into a profile's overlay/ tree. The path "
                "is relative to overlay/ (e.g. 'etc/hostname'). Parent "
                "directories are created."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "relpath": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["name", "relpath", "content"],
                "additionalProperties": False,
            },
        },
        {
            "name": "start_build",
            "description": (
                "Kick off `make image` for a profile/arch pair. Returns "
                "the new job_id; poll with get_build to watch state."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "profile": {"type": "string"},
                    "architecture": {
                        "type": "string", "default": "amd64",
                    },
                },
                "required": ["profile"],
                "additionalProperties": False,
            },
        },
        {
            "name": "list_builds",
            "description": (
                "Every build job the manager knows about, newest first."
            ),
            "inputSchema": {
                "type": "object", "properties": {}, "additionalProperties": False,
            },
        },
        {
            "name": "get_build",
            "description": (
                "Return a build job's state plus the last N lines of "
                "its log. `log_tail` defaults to 200."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "job_id": {"type": "string"},
                    "log_tail": {
                        "type": "integer", "minimum": 0, "default": 200,
                    },
                },
                "required": ["job_id"],
                "additionalProperties": False,
            },
        },
        {
            "name": "recent_events",
            "description": (
                "Recent boot events across the fleet (or a single MAC). "
                "Use for a live feel of what machines are doing."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "mac": {"type": "string"},
                    "limit": {
                        "type": "integer",
                        "minimum": 1, "maximum": 1000, "default": 200,
                    },
                },
                "additionalProperties": False,
            },
        },
    ]


def _build_tool_impls(
    *,
    registry: MachineRegistry,
    profiles_root: Path,
    builds: BuildJobManager,
) -> dict[str, Callable[[dict], dict]]:

    def _require(args: dict, key: str, kind: type) -> Any:
        if key not in args:
            raise _ToolError(f"missing argument: {key}")
        val = args[key]
        if not isinstance(val, kind):
            raise _ToolError(
                f"argument {key} must be {kind.__name__}"
            )
        return val

    def _profile_dir(name: str) -> Path:
        try:
            return _safe_profile_dir(profiles_root, name)
        except HTTPException as err:
            raise _ToolError(err.detail if isinstance(err.detail, str)
                             else "invalid profile name")

    def _machine_dict(m) -> dict:
        return {
            "mac": m.mac,
            "hostname": m.hostname,
            "profile_name": m.profile_name,
            "architecture": m.architecture,
            "platform": m.platform,
            "last_ip": m.last_ip,
            "boot_version": m.boot_version,
            "pending_reboot": bool(m.pending_reboot),
            "reboot_command": m.reboot_command,
            "scratch_mode": m.scratch_mode,
            "serial_console": bool(m.serial_console),
            "enrolled_by": m.enrolled_by,
        }

    def list_machines(_: dict) -> dict:
        return _tool_text_result(
            {"machines": [_machine_dict(m) for m in registry.list_all()]},
        )

    def get_machine(args: dict) -> dict:
        mac = _require(args, "mac", str)
        m = registry.lookup(mac)
        if m is None:
            raise _ToolError(f"no machine with MAC {mac!r}")
        return _tool_text_result(_machine_dict(m))

    def enrol_machine(args: dict) -> dict:
        mac = _require(args, "mac", str)
        profile_name = _require(args, "profile_name", str)
        architecture = args.get("architecture", "x86_64")
        platform = args.get("platform", "efi")
        serial_console = bool(args.get("serial_console", False))
        scratch_mode = args.get("scratch_mode", "volatile")
        if platform not in ("efi", "pc"):
            raise _ToolError("platform must be 'efi' or 'pc'")
        if scratch_mode not in ("volatile", "persistent", "off"):
            raise _ToolError("scratch_mode must be volatile/persistent/off")
        registry.enroll(
            mac=mac,
            profile_name=profile_name,
            architecture=architecture,
            platform=platform,
            serial_console=serial_console,
            scratch_mode=scratch_mode,
        )
        return _tool_text_result(_machine_dict(registry.lookup(mac)))

    def delete_machine(args: dict) -> dict:
        mac = _require(args, "mac", str)
        removed = registry.remove(mac)
        return _tool_text_result({"mac": mac, "removed": bool(removed)})

    def reboot(args: dict) -> dict:
        mac = _require(args, "mac", str)
        delete_after = bool(args.get("delete_after", False))
        reboot_machine(registry, mac, then_delete=delete_after)
        m = registry.lookup(mac)
        return _tool_text_result({
            "mac": mac,
            "deleted": m is None,
            "pending_reboot": bool(m.pending_reboot) if m else False,
        })

    def set_pending_reboot_tool(args: dict) -> dict:
        mac = _require(args, "mac", str)
        armed = bool(_require(args, "armed", bool))
        registry.set_pending_reboot(mac, armed)
        return _tool_text_result({"mac": mac, "pending_reboot": armed})

    def set_reboot_command_tool(args: dict) -> dict:
        mac = _require(args, "mac", str)
        command = _require(args, "command", str)
        registry.set_reboot_command(mac, command or None)
        return _tool_text_result({"mac": mac, "reboot_command": command or None})

    def list_profiles(_: dict) -> dict:
        return _tool_text_result({
            "profiles": _list_profile_names(profiles_root),
        })

    def _read(path: Path) -> str:
        return path.read_text() if path.is_file() else ""

    def get_profile(args: dict) -> dict:
        name = _require(args, "name", str)
        pdir = _profile_dir(name)
        if not pdir.is_dir():
            raise _ToolError(f"profile not found: {name}")
        parents = _read(pdir / "parent").strip().splitlines()
        return _tool_text_result({
            "name": name,
            "extra_packages": _read(pdir / "extra-packages.list"),
            "setup_chroot": _read(pdir / "setup-chroot"),
            "readme": _read(pdir / "README.md"),
            "parents": parents,
            "suite": _read(pdir / "suite").strip(),
        })

    def save_profile(args: dict) -> dict:
        name = _require(args, "name", str)
        pdir = _profile_dir(name)
        existed = pdir.is_dir()
        pdir.mkdir(parents=True, exist_ok=True)

        if "extra_packages" in args:
            (pdir / "extra-packages.list").write_text(
                args["extra_packages"] or ""
            )
        elif not existed:
            (pdir / "extra-packages.list").write_text("")

        if "readme" in args:
            (pdir / "README.md").write_text(args["readme"] or "")
        elif not existed:
            (pdir / "README.md").write_text("")

        if "setup_chroot" in args:
            script = args["setup_chroot"] or ""
            script_path = pdir / "setup-chroot"
            if script.strip():
                script_path.write_text(script)
                script_path.chmod(0o755)
            elif script_path.is_file():
                script_path.unlink()

        if "parents" in args:
            parents_arg = args["parents"] or []
            if not isinstance(parents_arg, list):
                raise _ToolError("parents must be a list of strings")
            known = set(_list_profile_names(profiles_root)) - {name}
            cleaned = [p for p in parents_arg if p in known]
            parent_path = pdir / "parent"
            if cleaned:
                parent_path.write_text("\n".join(cleaned) + "\n")
            elif parent_path.is_file():
                parent_path.unlink()

        if "suite" in args:
            suite_val = (args["suite"] or "").strip()
            suite_path = pdir / "suite"
            if suite_val:
                suite_path.write_text(suite_val + "\n")
            elif suite_path.is_file():
                suite_path.unlink()

        return _tool_text_result({
            "name": name,
            "created": not existed,
        })

    def write_overlay(args: dict) -> dict:
        name = _require(args, "name", str)
        relpath = _require(args, "relpath", str)
        content = _require(args, "content", str)
        pdir = _profile_dir(name)
        if not pdir.is_dir():
            raise _ToolError(f"profile not found: {name}")
        try:
            file_path = _safe_overlay_path(pdir, relpath)
        except HTTPException as err:
            raise _ToolError(
                err.detail if isinstance(err.detail, str)
                else "invalid overlay path"
            )
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content)
        return _tool_text_result({
            "profile": name,
            "relpath": relpath,
            "bytes_written": len(content.encode("utf-8")),
        })

    def start_build(args: dict) -> dict:
        profile = _require(args, "profile", str)
        architecture = args.get("architecture", "amd64")
        pdir = _profile_dir(profile)
        if not pdir.is_dir():
            raise _ToolError(f"profile not found: {profile}")
        try:
            job = builds.start(profile=profile, architecture=architecture)
        except BuildAlreadyRunningError as err:
            raise _ToolError(str(err) or "a build is already running")
        return _tool_text_result({
            "job_id": job.job_id,
            "profile": job.profile,
            "architecture": job.architecture,
            "state": job.state.value,
            "started_at": job.started_at,
        })

    def _job_dict(j) -> dict:
        return {
            "job_id": j.job_id,
            "profile": j.profile,
            "architecture": j.architecture,
            "state": j.state.value,
            "exit_code": j.exit_code,
            "started_at": j.started_at,
            "finished_at": j.finished_at,
        }

    def list_builds(_: dict) -> dict:
        return _tool_text_result({
            "running": builds.is_running(),
            "jobs": [_job_dict(j) for j in builds.list_jobs()],
        })

    def get_build(args: dict) -> dict:
        job_id = _require(args, "job_id", str)
        job = builds.get(job_id)
        if job is None:
            raise _ToolError(f"no build with job_id {job_id!r}")
        tail = int(args.get("log_tail", 200))
        return _tool_text_result({
            **_job_dict(job),
            "log_tail": builds.tail_log(job_id, n=tail),
        })

    def recent_events(args: dict) -> dict:
        mac = args.get("mac")
        limit = int(args.get("limit", 200))
        limit = max(1, min(limit, 1000))
        events = registry.recent_boot_events(limit=limit, mac=mac)
        return _tool_text_result({
            "events": [
                {
                    "occurred_at": e.occurred_at,
                    "mac": e.mac,
                    "state": e.state,
                    "detail": e.detail or "",
                }
                for e in events
            ],
        })

    return {
        "list_machines": list_machines,
        "get_machine": get_machine,
        "enrol_machine": enrol_machine,
        "delete_machine": delete_machine,
        "reboot_machine": reboot,
        "set_pending_reboot": set_pending_reboot_tool,
        "set_reboot_command": set_reboot_command_tool,
        "list_profiles": list_profiles,
        "get_profile": get_profile,
        "save_profile": save_profile,
        "write_profile_overlay": write_overlay,
        "start_build": start_build,
        "list_builds": list_builds,
        "get_build": get_build,
        "recent_events": recent_events,
    }
