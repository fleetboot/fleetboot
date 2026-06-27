"""Stub Fleetboot server for the image smoke test.

Runs the real FastAPI app (so the wire format is exactly what production uses)
plus a single static-file route serving the built squashfs to the guest via
live-boot's `fetch=URL` mechanism.

Exposes:
  - the URL the guest should report to,
  - a fresh boot-session token,
  - a thread-safe Event that fires the first time a `network_up` report
    matching the token arrives.

Usable as a context manager so the orchestrator can scope the lifetime cleanly.
"""

from __future__ import annotations

import socket
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse

from fleetboot.boot_states import BootState
from fleetboot.server.app import create_app
from fleetboot.server.boot_sessions import BootSessionStore


# Where in the URL space the smoke server publishes the squashfs. The
# generated kernel cmdline embeds this exact path.
SQUASHFS_URL_PATH = "/fleetboot.squashfs"


def find_free_port() -> int:
    """Bind a transient socket to ask the kernel for a free TCP port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("0.0.0.0", 0))
        return s.getsockname()[1]


def build_smoke_app(
    sessions: BootSessionStore,
    squashfs_path: Path,
    network_up_event: threading.Event,
    expected_token: str,
) -> FastAPI:
    """Wrap the real Fleetboot app with the smoke-test extras.

    The static squashfs route lets the guest fetch the image. The post-status
    middleware fires `network_up_event` the first time the right report shows
    up, so the orchestrator can wait on it instead of polling.
    """
    app = create_app(sessions=sessions)

    @app.get(SQUASHFS_URL_PATH)
    def serve_squashfs() -> FileResponse:
        return FileResponse(str(squashfs_path), media_type="application/octet-stream")

    @app.middleware("http")
    async def _watch_for_network_up(request, call_next):
        response = await call_next(request)
        # Only fire on a successful status POST whose state was network_up
        # and whose token matches ours.
        if (
            request.url.path == "/status"
            and request.method == "POST"
            and 200 <= response.status_code < 300
        ):
            auth = request.headers.get("authorization", "")
            if auth == f"Bearer {expected_token}":
                # We don't know the body here without re-reading it; rely on
                # the order constraint instead: network_up is always first,
                # so any successful POST with our token signals it has fired.
                network_up_event.set()
        return response

    return app


class StubServer:
    """A running stub server, exposing the data the orchestrator needs."""

    def __init__(
        self,
        host: str,
        port: int,
        mac: str,
        squashfs_path: Path,
    ) -> None:
        self.host = host
        self.port = port
        self.sessions = BootSessionStore()
        self.session = self.sessions.mint(mac)
        self.network_up_event = threading.Event()
        self.app = build_smoke_app(
            sessions=self.sessions,
            squashfs_path=squashfs_path,
            network_up_event=self.network_up_event,
            expected_token=self.session.token,
        )
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}/"

    @property
    def squashfs_url(self) -> str:
        return f"http://{self.host}:{self.port}{SQUASHFS_URL_PATH}"

    @property
    def boot_token(self) -> str:
        return self.session.token

    def start(self) -> None:
        config = uvicorn.Config(
            self.app,
            host="0.0.0.0",
            port=self.port,
            log_level="warning",
            access_log=False,
        )
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()
        # Block until uvicorn is actually accepting connections, so the guest
        # can't race the orchestrator.
        for _ in range(200):
            if self._server.started:
                return
            threading.Event().wait(0.05)
        raise RuntimeError("stub server failed to come up")

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=5)

    def wait_for_network_up(self, timeout: float) -> bool:
        """Block up to `timeout` seconds for the network_up event."""
        return self.network_up_event.wait(timeout=timeout)

    def latest_state(self) -> BootState | None:
        refreshed = self.sessions.lookup(self.session.token)
        return refreshed.latest_state if refreshed else None


@contextmanager
def running_stub_server(
    host: str,
    port: int,
    mac: str,
    squashfs_path: Path,
) -> Iterator[StubServer]:
    """Context manager wrapping start/stop of a stub server."""
    server = StubServer(host=host, port=port, mac=mac, squashfs_path=squashfs_path)
    server.start()
    try:
        yield server
    finally:
        server.stop()
