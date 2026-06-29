"""Tests for the FleetbootClient (mint + resolve) and the registry adapter."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from fleetboot.tftp_glue.client import (
    FleetbootClient,
    MintFailedError,
    RegisteredMachine,
    ResolveFailedError,
    build_registry_lookup,
)


# ---- Minimal fake httpx-shaped client ------------------------------------


@dataclass
class _FakeResponse:
    status_code: int
    body: Any = None
    text: str = ""

    def json(self) -> Any:
        return self.body


class _FakeHttpClient:
    """Records every call and returns scripted responses keyed by (method, url)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []
        self.responses: dict[tuple[str, str], _FakeResponse] = {}

    def set_response(self, method: str, url: str, response: _FakeResponse) -> None:
        self.responses[(method, url)] = response

    def post(self, url: str, *, json: dict, headers: dict) -> _FakeResponse:
        self.calls.append(("POST", url, {"json": json, "headers": headers}))
        return self.responses.get(("POST", url), _FakeResponse(status_code=500))

    def get(self, url: str, *, headers: dict) -> _FakeResponse:
        self.calls.append(("GET", url, {"headers": headers}))
        return self.responses.get(("GET", url), _FakeResponse(status_code=500))


# ---- mint_session --------------------------------------------------------


def test_mint_session_posts_and_returns_token():
    fake = _FakeHttpClient()
    fake.set_response(
        "POST", "http://fleet/sessions",
        _FakeResponse(status_code=201, body={"token": "abc123", "mac": "x"}),
    )
    client = FleetbootClient(
        base_url="http://fleet", mint_secret="s", http_client=fake,
    )
    token = client.mint_session("aa:bb:cc:dd:ee:ff")
    assert token == "abc123"
    method, url, kwargs = fake.calls[0]
    assert (method, url) == ("POST", "http://fleet/sessions")
    assert kwargs["json"] == {"mac": "aa:bb:cc:dd:ee:ff"}
    assert kwargs["headers"]["Authorization"] == "Bearer s"


def test_mint_session_raises_on_non_201():
    fake = _FakeHttpClient()
    fake.set_response(
        "POST", "http://fleet/sessions",
        _FakeResponse(status_code=401, text="nope"),
    )
    client = FleetbootClient(
        base_url="http://fleet", mint_secret="s", http_client=fake,
    )
    with pytest.raises(MintFailedError):
        client.mint_session("aa:bb:cc:dd:ee:ff")


# ---- lookup_machine ------------------------------------------------------


def test_lookup_machine_returns_registered_record():
    fake = _FakeHttpClient()
    fake.set_response(
        "GET", "http://fleet/resolve/aa:bb:cc:dd:ee:ff",
        _FakeResponse(
            status_code=200,
            body={
                "mac": "aa:bb:cc:dd:ee:ff",
                "profile_name": "lab",
                "architecture": "x86_64",
                "platform": "efi",
                "created_at": "2026-06-27T00:00:00",
            },
        ),
    )
    client = FleetbootClient(
        base_url="http://fleet", mint_secret="s", http_client=fake,
    )
    machine = client.lookup_machine("aa:bb:cc:dd:ee:ff")
    assert machine == RegisteredMachine(
        mac="aa:bb:cc:dd:ee:ff",
        profile_name="lab",
        architecture="x86_64",
        platform="efi",
    )


def test_lookup_machine_returns_none_on_404():
    fake = _FakeHttpClient()
    fake.set_response(
        "GET", "http://fleet/resolve/aa:bb:cc:dd:ee:00",
        _FakeResponse(status_code=404),
    )
    client = FleetbootClient(
        base_url="http://fleet", mint_secret="s", http_client=fake,
    )
    assert client.lookup_machine("aa:bb:cc:dd:ee:00") is None


def test_lookup_machine_raises_on_other_non_200():
    fake = _FakeHttpClient()
    fake.set_response(
        "GET", "http://fleet/resolve/aa:bb:cc:dd:ee:ff",
        _FakeResponse(status_code=503, text="not configured"),
    )
    client = FleetbootClient(
        base_url="http://fleet", mint_secret="s", http_client=fake,
    )
    with pytest.raises(ResolveFailedError):
        client.lookup_machine("aa:bb:cc:dd:ee:ff")


# ---- build_registry_lookup adapter ---------------------------------------


def test_registry_lookup_returns_truthy_for_known_mac():
    fake = _FakeHttpClient()
    fake.set_response(
        "GET", "http://fleet/resolve/aa:bb:cc:dd:ee:ff",
        _FakeResponse(
            status_code=200,
            body={
                "mac": "aa:bb:cc:dd:ee:ff",
                "profile_name": "lab",
                "architecture": "x86_64",
                "platform": "efi",
                "created_at": "2026-06-27T00:00:00",
            },
        ),
    )
    client = FleetbootClient(
        base_url="http://fleet", mint_secret="s", http_client=fake,
    )
    lookup = build_registry_lookup(client)
    result = lookup("aa:bb:cc:dd:ee:ff")
    assert result is not None
    assert result.profile_name == "lab"


def test_registry_lookup_returns_none_for_unknown_mac():
    fake = _FakeHttpClient()
    fake.set_response(
        "GET", "http://fleet/resolve/aa:bb:cc:dd:ee:00",
        _FakeResponse(status_code=404),
    )
    client = FleetbootClient(
        base_url="http://fleet", mint_secret="s", http_client=fake,
    )
    lookup = build_registry_lookup(client)
    assert lookup("aa:bb:cc:dd:ee:00") is None
