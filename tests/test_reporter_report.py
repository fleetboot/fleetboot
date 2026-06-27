"""End-to-end test: the reporter posts through the real FastAPI app.

We do not hit a real network — httpx's ASGITransport routes the request
directly into the app object. This exercises the wire format, headers, status
codes, and the session store, all together.
"""

import pytest
from fastapi.testclient import TestClient

from openschool.boot_states import BootState
from openschool.reporter.cmdline import ReporterSettings
from openschool.reporter.report import ReportFailedError, main, report_state
from openschool.server.app import create_app
from openschool.server.boot_sessions import BootSessionStore


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
            BootState.LOGIN_READY, settings=settings, client=client
        )
        report_state(
            BootState.USER_LOGGED_IN,
            detail="alice",
            settings=settings,
            client=client,
        )
    refreshed = store.lookup(session.token)
    assert refreshed is not None
    assert refreshed.reports == [
        BootState.NETWORK_UP,
        BootState.NFS_MOUNTED,
        BootState.LOGIN_READY,
        BootState.USER_LOGGED_IN,
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


def test_main_cli_rejects_unknown_state(capsys):
    exit_code = main(["rooted_the_box"])
    assert exit_code == 2
    err = capsys.readouterr().err
    assert "unknown state" in err


def test_main_cli_usage_error_with_no_args(capsys):
    exit_code = main([])
    assert exit_code == 2
    err = capsys.readouterr().err
    assert "usage" in err
