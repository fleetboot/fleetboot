"""End-to-end test: the reporter posts through the real FastAPI app.

We do not hit a real network — httpx's ASGITransport routes the request
directly into the app object. This exercises the wire format, headers, status
codes, and the session store, all together.
"""

import pytest
from fastapi.testclient import TestClient

from fleetboot.boot_states import BootState
from fleetboot.reporter.cmdline import ReporterSettings
from fleetboot.reporter.report import ReportFailedError, main, report_state
from fleetboot.server.app import create_app
from fleetboot.server.boot_sessions import BootSessionStore


def _wired_client(store: BootSessionStore) -> TestClient:
    """A FastAPI TestClient that hits the real app in-process.

    TestClient is built on httpx and exposes the same .post(url, json=, headers=)
    surface report_state() needs, so it drops straight in as the injected
    client.
    """
    app = create_app(sessions=store)
    return TestClient(app)


def test_reporter_posts_and_server_records():
    store = BootSessionStore()
    session = store.mint("aa:bb:cc:dd:ee:ff")
    settings = ReporterSettings(
        server_url="http://testserver/", boot_token=session.token
    )
    with _wired_client(store) as client:
        report_state(
            BootState.NETWORK_UP, settings=settings, client=client
        )
        report_state(
            BootState.NFS_MOUNTED, settings=settings, client=client
        )
        report_state(
            BootState.LOGIN_CONSOLE, settings=settings, client=client
        )
        report_state(
            BootState.LOGIN_CONSOLE,
            detail="alice",
            settings=settings,
            client=client,
        )
    refreshed = store.lookup(session.token)
    assert refreshed is not None
    assert refreshed.reports == [
        BootState.NETWORK_UP,
        BootState.NFS_MOUNTED,
        BootState.LOGIN_CONSOLE,
        BootState.LOGIN_CONSOLE,
    ]


def test_reporter_raises_on_unknown_token():
    store = BootSessionStore()
    settings = ReporterSettings(
        server_url="http://testserver/", boot_token="not-a-real-token"
    )
    with _wired_client(store) as client:
        with pytest.raises(ReportFailedError):
            report_state(
                BootState.NETWORK_UP, settings=settings, client=client
            )


def test_main_cli_accepts_custom_state_string(capsys, monkeypatch):
    """Any short identifier-shaped state is accepted — custom states
    used by profile hooks (e.g. github-runner's `runner_started`)
    aren't in the BootState enum but must still reach the server."""
    # Substitute a report_state that succeeds without hitting the
    # network so we can assert the CLI accepts the value.
    seen = {}

    def fake_report_state(state, detail=None):
        seen["state"] = state
        seen["detail"] = detail

    monkeypatch.setattr(
        "fleetboot.reporter.report.report_state", fake_report_state,
    )
    assert main(["runner_started"]) == 0
    assert seen["state"] == "runner_started"


def test_main_cli_rejects_malformed_state(capsys):
    """Empty, whitespace-only, or absurdly long state strings are
    still rejected — a malformed value can't be a meaningful state."""
    for evil in ("", "   ", "has spaces", "a" * 65):
        exit_code = main([evil])
        assert exit_code == 2, f"{evil!r} should have been rejected"


def test_main_cli_usage_error_with_no_args(capsys):
    exit_code = main([])
    assert exit_code == 2
    err = capsys.readouterr().err
    assert "usage" in err


def test_reporter_acks_soft_reboot_with_rebooting_state(monkeypatch, tmp_path):
    """When /status replies with pending_reboot=true the reporter must
    (a) POST a `rebooting` custom state so the dashboard shows the
    signal was acknowledged, and (b) invoke systemctl reboot. The
    ack POST must NOT recurse (its own reply also carries
    pending_reboot=true until the machine PXE-mints)."""
    from pathlib import Path
    from fleetboot.server.registry import MachineRegistry

    store = BootSessionStore()
    registry = MachineRegistry(Path(tmp_path) / "machines.sqlite")
    registry.enroll(
        mac="aa:bb:cc:dd:ee:ff", profile_name="default",
        architecture="x86_64", platform="efi",
    )
    registry.set_pending_reboot("aa:bb:cc:dd:ee:ff", True)
    session = store.mint("aa:bb:cc:dd:ee:ff")
    settings = ReporterSettings(
        server_url="http://testserver/", boot_token=session.token,
    )
    reboot_calls = []

    # Substitute subprocess.Popen ONLY for the systemctl-reboot call
    # in report.py — other Popen callers in the reporter (e.g.
    # subprocess.run inside diagnostics collection) still need real
    # subprocesses to succeed.
    import subprocess as _subprocess
    real_popen = _subprocess.Popen

    class _FakeProc:
        returncode = 0
        def __enter__(self): return self
        def __exit__(self, *_): return False
        def wait(self, timeout=None): return 0
        def communicate(self, *_a, **_kw): return (b"", b"")

    def fake_popen(argv, *a, **kw):
        # ONLY intercept the exact reboot invocations from report.py.
        # The reporter also shells out to `systemctl list-units` etc.
        # for diagnostics via subprocess.run (which internally calls
        # Popen); those must reach the real Popen unmodified.
        if argv == ["systemctl", "reboot"] or argv == ["reboot"]:
            reboot_calls.append(argv)
            return _FakeProc()
        return real_popen(argv, *a, **kw)

    monkeypatch.setattr(_subprocess, "Popen", fake_popen)
    # Suppress the "current-state" file write which needs /run/fleetboot.
    monkeypatch.setattr(
        "fleetboot.reporter.report._remember_current_state",
        lambda _s: None,
    )
    app = create_app(sessions=store, registry=registry)
    with TestClient(app) as client:
        report_state(
            BootState.NETWORK_UP, settings=settings, client=client,
        )
    # systemctl reboot fired.
    assert reboot_calls == [["systemctl", "reboot"]]
    # `rebooting` state landed as a boot event on the machine row.
    events = registry.recent_boot_events(mac="aa:bb:cc:dd:ee:ff")
    assert any(e.state == "rebooting" for e in events), (
        f"expected `rebooting` in {[e.state for e in events]}"
    )
