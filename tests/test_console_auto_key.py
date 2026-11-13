"""The console on this computer signs itself in (Nov 2026, granny test).

/api/console-key hands FreeHand's API key only to its own page opened on
this computer. Everything else - another host name, another site, a hosted
tenant - is refused."""
import pytest
from fastapi.testclient import TestClient

from server import app, get_or_create_api_key


@pytest.fixture
def client():
    return TestClient(app)


def get(client, host, **headers):
    return client.get("/api/console-key", headers={"host": host, **headers})


@pytest.mark.parametrize("host", ["127.0.0.1:8000", "localhost:8000", "localhost", "[::1]:8000"])
def test_console_on_this_computer_gets_the_key(client, host):
    r = get(client, host, **{"sec-fetch-site": "same-origin"})
    assert r.status_code == 200
    assert r.json()["key"] == get_or_create_api_key()
    assert r.headers["cache-control"] == "no-store"


def test_the_key_then_works(client):
    key = get(client, "127.0.0.1:8000").json()["key"]
    assert client.get("/api/memory/status", headers={"X-API-Key": key}).status_code == 200


@pytest.mark.parametrize("host", ["freehand.example.com", "evil.test:8000", "127.0.0.1.evil.test"])
def test_other_host_names_are_refused(client, host):
    """DNS rebinding keeps the attacker's host name, so it lands here."""
    r = get(client, host)
    assert r.status_code == 403 and "key" not in r.json()


def test_other_sites_are_refused(client):
    r = get(client, "127.0.0.1:8000", **{"sec-fetch-site": "cross-site", "origin": "https://evil.test"})
    assert r.status_code == 403


def test_mismatched_origin_is_refused(client):
    r = get(client, "127.0.0.1:8000", origin="http://localhost:9999")
    assert r.status_code == 403


def test_hosted_tenants_can_switch_it_off(client, monkeypatch):
    monkeypatch.setenv("FREEHAND_CONSOLE_AUTO_KEY", "0")
    assert get(client, "127.0.0.1:8000").status_code == 403
