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
from fleetboot.server.boot_sessions import BootSessionStore
from fleetboot.server.dashboard import (
    _STALE_AFTER_SECONDS,
    POWER_ACTIONS,
    _list_profile_names,
    _safe_overlay_path,
    _safe_profile_dir,
    power_machine,
    reboot_machine,
)
from fleetboot.server import mcp_docs
from fleetboot.server.registry import (
    MachineRegistry,
    matches_architecture,
)


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

It serves two jobs from the same registry:

  Fleet control — keep a set of known machines running a known image:
  enrol them, roll out new images, watch them boot, recover the ones
  that wedge.

  On-demand compute — treat that same hardware as a pool you switch
  on when there's work and off when there isn't. `power_fleet` takes
  a profile and a count, `list_machines` tells you which ones came up
  (`online`, `state`), and powering off afterwards costs nothing to
  undo: the image is immutable, so a machine that comes back is
  identical to the one you switched off. The `claude-compute` profile
  is a ready-made worker for exactly this — SSH in, run the job, power
  it down. Call `describe_fleetboot(topic="on-demand-compute")` for the
  full pattern.

  If you are an agent using this fleet as compute, you own the power
  bill: switch machines ON only when you have work for them, and switch
  them back OFF the moment the work is done. A worker you left running
  idle wastes real power and holds hardware other work could use — do
  not leave the pool up "in case". `power_fleet` with action="off" and
  the profile releases everything you brought up; the immutable image
  means the next power-on is identical, so there is no reason to hoard.

Typical workflows an agent can drive here:

  Add a machine to the fleet
    1. `list_profiles` to see what's on offer.
    2. `enrol_machine` with its MAC + profile_name.
    3. Optionally `set_reboot_command` if this box has its own PDU.

  Create or tweak a profile, then deploy it
    1. `save_profile` (creates or updates the directory).
    2. `write_profile_overlay` for any files that ship inside the image.
    3. `start_build` with `auto_reboot=true` — on success the manager
       arms the soft-reboot signal on every machine running the same
       profile+arch, so they PXE the new image on their next
       heartbeat with no per-machine calls from you.
    4. Poll `get_build` until state is "succeeded"; `auto_reboot_armed`
       reports how many machines were swept.

  Power a machine on, off, or through a reboot
    - `power_machine` with action on/off/reboot. Each action tries the
      machine's own command for it, then the fleet-wide pdudaemon
      (/power/control/<action>?alias=<alias>). Check `ok` in the
      result — power control fails quietly in the real world.
    - Only `reboot` has a soft fallback (arming the in-image reboot
      signal). `on` and `off` need real power control; if none is
      wired up they report failure rather than pretending.
    - `power_fleet` does the same across a profile, with an optional
      count — that's the on-demand lever.
    - `reboot_machine` is the reboot-only shorthand. Pass
      delete_after=true only when you also want the registry row gone
      (a soft-reboot cannot deliver to a deleted row).

  Watch the fleet
    - `list_machines` for a snapshot; `recent_events` for a live feel
      of what states machines are hitting.

State is durable: profile edits land on disk, enrolments land in
SQLite, builds produce artefacts under build/. There is no undo — a
delete or a save overwrites in place.

This server documents itself. Before doing anything non-obvious — or
whenever you need to explain fleetboot to someone — call
`describe_fleetboot`, which serves full documentation with worked
examples: what the system is (`overview`), everything it offers
(`services`), how images are composed (`profiles`), step-by-step
recipes (`workflows`), what each boot state means when a machine
stalls (`boot-lifecycle`), and the raw wire protocol (`examples`).
The same pages are available as MCP resources under
`fleetboot://docs/<topic>`.
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


def _tool_markdown_result(text: str) -> dict:
    """Wrap prose as MCP tool content: one markdown text block.

    Documentation goes back as-is rather than JSON-encoded — a model
    reads markdown better than an escaped string, and there is no
    structure here worth parsing.
    """
    return {
        "content": [{"type": "text", "text": text}],
        "isError": False,
    }


def build_mcp_router(
    *,
    registry: MachineRegistry,
    profiles_root: Path,
    builds: BuildJobManager,
    admin_secret: str,
    sessions: Optional[BootSessionStore] = None,
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
        sessions=sessions,
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
                "capabilities": {
                    "tools": {"listChanged": False},
                    # The documentation topics. Static, so nothing to
                    # subscribe to and no change notifications.
                    "resources": {"listChanged": False, "subscribe": False},
                },
                "instructions": SERVER_INSTRUCTIONS,
            }
        elif method == "notifications/initialized" or method == "initialized":
            # Notification — no response.
            return None
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": tools}
        elif method == "resources/list":
            result = {"resources": mcp_docs.resource_descriptors()}
        elif method == "resources/templates/list":
            # No parameterised resources — answer the question rather
            # than making a conformant client handle a method error.
            result = {"resourceTemplates": []}
        elif method == "resources/read":
            uri = params.get("uri")
            topic = (
                mcp_docs.topic_for_uri(uri) if isinstance(uri, str) else None
            )
            if topic is None:
                return None if is_notification else _rpc_error(
                    id_, _INVALID_PARAMS,
                    f"unknown resource: {uri!r}. Known resources: "
                    + ", ".join(
                        t["uri"] for t in mcp_docs.resource_descriptors()
                    ),
                )
            result = {
                "contents": [
                    {
                        "uri": topic.uri,
                        "name": topic.name,
                        "title": topic.title,
                        "mimeType": mcp_docs.DOC_MIME_TYPE,
                        "text": topic.body + mcp_docs.topic_index(),
                    },
                ],
            }
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
            "name": "describe_fleetboot",
            "description": (
                "Read fleetboot's own documentation: what the system "
                "is, what it can do, what services it offers, and "
                "worked examples. Call this first when you are new to "
                "this server, when a user asks what fleetboot is or "
                "what it can do, or before any non-obvious sequence of "
                "calls. Returns markdown. Topics: "
                + ", ".join(
                    f"{topic.name} ({topic.summary.rstrip('.')})"
                    for topic in mcp_docs.TOPICS
                )
                + "."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "topic": {
                        "type": "string",
                        "enum": mcp_docs.topic_names(),
                        "default": mcp_docs.DEFAULT_TOPIC,
                        "description": (
                            "Which documentation page to read. "
                            "Defaults to the overview."
                        ),
                    },
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "list_machines",
            "description": (
                "List every enrolled machine — MAC, hostname, profile, "
                "architecture, last observed IP, boot version, power "
                "wiring, and whether a soft-reboot signal is armed. "
                "Also carries the live view: `state` (how far the "
                "current boot got), `last_seen`, and `online` (it has "
                "heartbeated in the last 6 minutes). Use `online` and "
                "`state == 'login_console'` to tell when a machine you "
                "powered on is actually ready for work."
            ),
            "inputSchema": {
                "type": "object", "properties": {}, "additionalProperties": False,
            },
        },
        {
            "name": "get_machine",
            "description": (
                "Return the full record for a single MAC, including its "
                "live boot state and whether it is currently online."
            ),
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
            "name": "set_machine_profile",
            "description": (
                "Move an already-enrolled machine onto a different "
                "profile — the way to change what a machine boots. "
                "Only the profile changes: architecture, platform, "
                "scratch mode and power wiring survive, unlike "
                "`enrol_machine`, which replaces the row. The machine "
                "keeps running its current image until it next boots, "
                "so follow with `reboot_machine` (or "
                "`set_pending_reboot`) if the change should land now."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "mac": {"type": "string"},
                    "profile_name": {"type": "string"},
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
            "name": "power_machine",
            "description": (
                "Switch one machine on, off, or through a reboot. Tries "
                "the machine's own command for that action, then the "
                "fleet-wide pdudaemon "
                "(/power/control/<action>?alias=<alias>). Only `reboot` "
                "has a soft fallback (arming the in-image reboot "
                "signal); `on` and `off` need real power control and "
                "report failure honestly if none is wired up. The "
                "result says which route was taken and whether it "
                "worked — check `ok`, don't assume."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "mac": {"type": "string"},
                    "action": {
                        "type": "string",
                        "enum": list(POWER_ACTIONS),
                    },
                },
                "required": ["mac", "action"],
                "additionalProperties": False,
            },
        },
        {
            "name": "power_fleet",
            "description": (
                "Switch a whole class of machines at once — the "
                "on-demand compute lever. Selects enrolled machines by "
                "profile (optionally architecture), then powers "
                "`count` of them (all matching, if count is omitted). "
                "Selection is idempotent: `on` only targets machines "
                "that aren't already online, `off` and `reboot` only "
                "ones that are, so asking twice for four workers "
                "doesn't cycle the four you already have. Returns a "
                "per-machine result plus `short_by` when the fleet "
                "couldn't supply the count you asked for."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": list(POWER_ACTIONS),
                    },
                    "profile": {
                        "type": "string",
                        "description": "Only machines on this profile.",
                    },
                    "architecture": {
                        "type": "string",
                        "description": (
                            "Optional further filter. amd64/x86_64/x64 "
                            "are treated as one architecture, as are "
                            "arm64/aarch64."
                        ),
                    },
                    "count": {
                        "type": "integer",
                        "minimum": 1,
                        "description": (
                            "How many machines to switch. Omit for "
                            "every eligible machine."
                        ),
                    },
                },
                "required": ["action", "profile"],
                "additionalProperties": False,
            },
        },
        {
            "name": "set_power_command",
            "description": (
                "Set (or clear, with empty string) the shell command "
                "that performs one power action for a machine. Runs on "
                "the fleetboot host with shell=True, and overrides "
                "pdudaemon for that action. Use for hardware pdudaemon "
                "doesn't cover — IPMI, a cloud API, wake-on-LAN."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "mac": {"type": "string"},
                    "action": {
                        "type": "string",
                        "enum": list(POWER_ACTIONS),
                    },
                    "command": {"type": "string"},
                },
                "required": ["mac", "action", "command"],
                "additionalProperties": False,
            },
        },
        {
            "name": "set_pdu_alias",
            "description": (
                "Set (or clear, with empty string) the name this "
                "machine answers to on pdudaemon. Defaults to the "
                "machine's reported hostname — but a machine that has "
                "never booted has no hostname, and powering one of "
                "those on is exactly the on-demand case, so set the "
                "alias at enrolment time."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "mac": {"type": "string"},
                    "alias": {"type": "string"},
                },
                "required": ["mac", "alias"],
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
                "the new job_id; poll with get_build to watch state. "
                "If the profile is inherited from, this also rebuilds "
                "every downstream profile (their images embed this "
                "profile's chain, so they'd be stale otherwise); the "
                "result's `cascade` lists every job, run one at a time. "
                "Pass `auto_reboot=true` to arm the soft-reboot signal "
                "on every machine running the same profile+arch when "
                "each build succeeds — they PXE the new image on their "
                "next heartbeat."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "profile": {"type": "string"},
                    "architecture": {
                        "type": "string", "default": "amd64",
                    },
                    "auto_reboot": {
                        "type": "boolean", "default": False,
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


def _liveness_by_mac(
    sessions: Optional[BootSessionStore],
) -> dict[str, dict]:
    """How alive each machine looks, keyed by MAC.

    An agent that just switched a machine on needs to know when it is
    usable, and the only evidence the control plane has is the boot
    session: the furthest state that boot reached, and how long ago the
    machine last said anything. A machine that has stopped heartbeating
    is off, wedged, or off the network — from here they look the same,
    so `online` claims no more than "it spoke to us recently".
    """
    from datetime import datetime, timezone

    if sessions is None:
        return {}

    states: dict[str, str] = {}
    for session in sessions.active_sessions():
        if session.latest_state is not None:
            states[session.mac] = session.latest_state.value

    now = datetime.now(timezone.utc)
    out: dict[str, dict] = {}
    for mac, raw_timestamp in sessions.last_seen_by_mac().items():
        try:
            # SQLite's datetime('now') gives "YYYY-MM-DD HH:MM:SS" UTC.
            seen_at = datetime.fromisoformat(raw_timestamp).replace(
                tzinfo=timezone.utc,
            )
        except ValueError:
            continue
        seconds = max(0, int((now - seen_at).total_seconds()))
        out[mac] = {
            "state": states.get(mac),
            "last_seen": raw_timestamp,
            "seconds_since_last_seen": seconds,
            "online": seconds < _STALE_AFTER_SECONDS,
        }
    for mac, state in states.items():
        out.setdefault(mac, {
            "state": state,
            "last_seen": None,
            "seconds_since_last_seen": None,
            "online": False,
        })
    return out


def _build_tool_impls(
    *,
    registry: MachineRegistry,
    profiles_root: Path,
    builds: BuildJobManager,
    sessions: Optional[BootSessionStore] = None,
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

    _OFFLINE = {
        "state": None,
        "last_seen": None,
        "seconds_since_last_seen": None,
        "online": False,
    }

    def _machine_dict(m, liveness: Optional[dict] = None) -> dict:
        live = (liveness or {}).get(m.mac, _OFFLINE)
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
            "power_on_command": m.power_on_command,
            "power_off_command": m.power_off_command,
            "pdu_alias": m.pdu_alias,
            "scratch_mode": m.scratch_mode,
            "serial_console": bool(m.serial_console),
            "enrolled_by": m.enrolled_by,
            # Live view: how far this machine's current boot got, and
            # whether it is still talking to us.
            "state": live["state"],
            "last_seen": live["last_seen"],
            "seconds_since_last_seen": live["seconds_since_last_seen"],
            "online": live["online"],
        }

    def describe_fleetboot(args: dict) -> dict:
        name = args.get("topic", mcp_docs.DEFAULT_TOPIC)
        if not isinstance(name, str):
            raise _ToolError("topic must be a string")
        topic = mcp_docs.get_topic(name)
        if topic is None:
            raise _ToolError(
                f"unknown topic: {name!r}. Known topics: "
                + ", ".join(mcp_docs.topic_names())
            )
        return _tool_markdown_result(topic.body + mcp_docs.topic_index())

    def list_machines(_: dict) -> dict:
        liveness = _liveness_by_mac(sessions)
        return _tool_text_result(
            {"machines": [
                _machine_dict(m, liveness) for m in registry.list_all()
            ]},
        )

    def get_machine(args: dict) -> dict:
        mac = _require(args, "mac", str)
        m = registry.lookup(mac)
        if m is None:
            raise _ToolError(f"no machine with MAC {mac!r}")
        return _tool_text_result(
            _machine_dict(m, _liveness_by_mac(sessions)),
        )

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
        # Enrolling onto a profile that doesn't exist fails at boot, on
        # the machine, long after the call that caused it.
        known = _list_profile_names(profiles_root)
        if profile_name not in known:
            raise _ToolError(
                f"unknown profile: {profile_name!r}. Known profiles: "
                + (", ".join(known) or "(none)")
            )
        registry.enroll(
            mac=mac,
            profile_name=profile_name,
            architecture=architecture,
            platform=platform,
            serial_console=serial_console,
            scratch_mode=scratch_mode,
        )
        return _tool_text_result(_machine_dict(registry.lookup(mac)))

    def set_machine_profile(args: dict) -> dict:
        mac = _require(args, "mac", str)
        profile_name = _require(args, "profile_name", str)
        known = _list_profile_names(profiles_root)
        if profile_name not in known:
            raise _ToolError(
                f"unknown profile: {profile_name!r}. Known profiles: "
                + ", ".join(known)
            )
        if not registry.set_profile(mac, profile_name):
            raise _ToolError(f"no machine with MAC {mac!r}")
        return _tool_text_result(
            _machine_dict(registry.lookup(mac), _liveness_by_mac(sessions)),
        )

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

    def _power_result_dict(result) -> dict:
        return {
            "mac": result.mac,
            "action": result.action,
            "method": result.method,
            "ok": result.ok,
            "detail": result.detail,
            "deleted": result.deleted,
        }

    def power(args: dict) -> dict:
        mac = _require(args, "mac", str)
        action = _require(args, "action", str)
        if action not in POWER_ACTIONS:
            raise _ToolError(
                f"action must be one of {', '.join(POWER_ACTIONS)}"
            )
        if registry.lookup(mac) is None:
            raise _ToolError(f"no machine with MAC {mac!r}")
        return _tool_text_result(
            _power_result_dict(power_machine(registry, mac, action)),
        )

    def power_fleet(args: dict) -> dict:
        action = _require(args, "action", str)
        profile = _require(args, "profile", str)
        if action not in POWER_ACTIONS:
            raise _ToolError(
                f"action must be one of {', '.join(POWER_ACTIONS)}"
            )
        architecture = args.get("architecture")
        if architecture is not None and not isinstance(architecture, str):
            raise _ToolError("architecture must be a string")
        count = args.get("count")
        if count is not None:
            if not isinstance(count, int) or isinstance(count, bool):
                raise _ToolError("count must be an integer")
            if count < 1:
                raise _ToolError("count must be at least 1")

        liveness = _liveness_by_mac(sessions)
        matching = [
            m for m in registry.list_all()
            if m.profile_name == profile
            and (
                architecture is None
                or matches_architecture(m.architecture, architecture)
            )
        ]
        # Switching on only makes sense for machines that aren't already
        # up, and switching off only for ones that are — so a repeated
        # "give me four" is idempotent rather than cycling live work.
        def _is_online(machine) -> bool:
            return bool(liveness.get(machine.mac, _OFFLINE)["online"])

        if action == "on":
            candidates = [m for m in matching if not _is_online(m)]
        else:
            candidates = [m for m in matching if _is_online(m)]
        # Deterministic order so a partial request is repeatable.
        candidates.sort(key=lambda m: m.mac)
        selected = candidates if count is None else candidates[:count]

        results = [
            _power_result_dict(power_machine(registry, m.mac, action))
            for m in selected
        ]
        succeeded = sum(1 for r in results if r["ok"])
        shortfall = (
            max(0, count - len(selected)) if count is not None else 0
        )
        return _tool_text_result({
            "action": action,
            "profile": profile,
            "architecture": architecture,
            "matching": len(matching),
            "eligible": len(candidates),
            "selected": len(selected),
            "succeeded": succeeded,
            "short_by": shortfall,
            "results": results,
        })

    def set_power_command_tool(args: dict) -> dict:
        mac = _require(args, "mac", str)
        action = _require(args, "action", str)
        command = _require(args, "command", str)
        if action not in POWER_ACTIONS:
            raise _ToolError(
                f"action must be one of {', '.join(POWER_ACTIONS)}"
            )
        if registry.lookup(mac) is None:
            raise _ToolError(f"no machine with MAC {mac!r}")
        registry.set_power_command(mac, action, command)
        machine = registry.lookup(mac)
        return _tool_text_result({
            "mac": mac,
            "action": action,
            "command": {
                "on": machine.power_on_command,
                "off": machine.power_off_command,
                "reboot": machine.reboot_command,
            }[action],
        })

    def set_pdu_alias_tool(args: dict) -> dict:
        mac = _require(args, "mac", str)
        alias = _require(args, "alias", str)
        if registry.lookup(mac) is None:
            raise _ToolError(f"no machine with MAC {mac!r}")
        registry.set_pdu_alias(mac, alias)
        return _tool_text_result({
            "mac": mac,
            "pdu_alias": registry.lookup(mac).pdu_alias,
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
        auto_reboot = bool(args.get("auto_reboot", False))
        pdir = _profile_dir(profile)
        if not pdir.is_dir():
            raise _ToolError(f"profile not found: {profile}")
        try:
            jobs = builds.start_with_downstream(
                profile=profile,
                architecture=architecture,
                auto_reboot=auto_reboot,
            )
        except BuildAlreadyRunningError as err:
            raise _ToolError(str(err) or "a build is already running")
        primary = jobs[0]
        result = {
            "job_id": primary.job_id,
            "profile": primary.profile,
            "architecture": primary.architecture,
            "state": primary.state.value,
            "started_at": primary.started_at,
            "auto_reboot": primary.auto_reboot,
        }
        # When the profile has descendants, building it also rebuilds
        # them (their images embed its chain). Report the whole cascade
        # so the caller can poll each job, not just the root.
        if len(jobs) > 1:
            result["cascade"] = [
                {"job_id": j.job_id, "profile": j.profile}
                for j in jobs
            ]
        return _tool_text_result(result)

    def _job_dict(j) -> dict:
        return {
            "job_id": j.job_id,
            "profile": j.profile,
            "architecture": j.architecture,
            "state": j.state.value,
            "exit_code": j.exit_code,
            "started_at": j.started_at,
            "finished_at": j.finished_at,
            "auto_reboot": j.auto_reboot,
            "auto_reboot_armed": j.auto_reboot_armed,
            "cascade_root": j.cascade_root,
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
        "describe_fleetboot": describe_fleetboot,
        "list_machines": list_machines,
        "get_machine": get_machine,
        "enrol_machine": enrol_machine,
        "set_machine_profile": set_machine_profile,
        "delete_machine": delete_machine,
        "reboot_machine": reboot,
        "power_machine": power,
        "power_fleet": power_fleet,
        "set_power_command": set_power_command_tool,
        "set_pdu_alias": set_pdu_alias_tool,
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
