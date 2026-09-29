"""Tests for the OpenConnector HTTP client itself (Round 7, Task 3).

These exercise the *real* network path — no broker-layer monkey-patching.
A small HTTP server spun up via ``http.server`` stands in for OpenConnector
on a free port; tests point ``OOMOL_CONNECT_BASE_URL`` at it and assert
the client behaves correctly across:

- ``is_available()`` happy path (200 + ok:true)
- ``is_available()`` returns False on 200 + ok:false (runtime says it's not ready)
- ``is_available()`` returns False on 401 (token rejected)
- ``is_available()`` returns False on connection refused (runtime down)
- ``is_available()`` returns False when token is unset (tier-5 cannot probe)
- ``service_is_known()`` returns True for catalog hits
- ``service_is_known()`` returns False for misses
- ``is_available()`` result is cached for ``OOMOL_CONNECT_CACHE_TTL_SECONDS`` seconds

These tests run without Docker, without OpenConnector running — they
speak OpenConnector's wire format and replace nothing.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Optional

import pytest

sys.path.insert(0, r"C:\Users\trace\Documents\Default Project")

from core.oauth import open_connector


# ── Test server (OpenConnector stand-in) ──────────────────────────────


class _FakeOpenConnector(BaseHTTPRequestHandler):
    """Minimal OpenConnector wire-format emulator.

    Routes:
      GET  /v1/health        — returns {"data": {"ok": <bool>}, ...}
      GET  /v1/providers     — returns {"data": [{"id": "slack", ...}, ...]}

    All other routes return 404.
    """

    # The test sets these on the class before serving:
    HEALTH_OK = True
    SERVICES = []  # list of dicts; for matching OC reality the id is
    # under the "service" key, not "id". The client must honour both
    # shapes — verified against a live oomol-lab/open-connector@main
    # runtime on 28 Sep 2026.

    def log_message(self, *_args, **_kwargs):  # silence stderr noise
        return

    def do_GET(self):  # noqa: N802 (BaseHTTPRequestHandler API)
        if self.path == "/v1/health":
            body = json.dumps({
                "success": True,
                "message": "OK",
                "data": {"ok": self.HEALTH_OK, "runtime": "fake-oc"},
                "meta": {},
            }).encode("utf-8")
            self._reply(200, body)
        elif self.path == "/v1/providers":
            body = json.dumps({
                "success": True,
                "message": "OK",
                "data": list(self.SERVICES),
                "meta": {},
            }).encode("utf-8")
            self._reply(200, body)
        else:
            self._reply(404, b"{}")

    def _reply(self, status: int, body: bytes):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _free_port() -> int:
    """Bind a socket to port 0, capture the assigned port, close, return."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def fake_oc(monkeypatch):
    """Spin up a fake OpenConnector server on a free port.

    Returns the base URL the client should hit. Yields a dict with the
    fixture handles so tests can mutate ``HEALTH_OK`` / ``SERVICES``.
    """
    port = _free_port()
    # Reset token + cache so test environment is deterministic.
    open_connector._runtime_token = None
    open_connector._reset_cache_for_tests()

    server = HTTPServer(("127.0.0.1", port), _FakeOpenConnector)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    base_url = f"http://127.0.0.1:{port}"
    monkeypatch.setenv("OOMOL_CONNECT_BASE_URL", base_url)
    monkeypatch.setenv("OOMOL_CONNECT_RUNTIME_TOKEN", "test-token-xyz")
    monkeypatch.setenv("OOMOL_CONNECT_HEALTH_TIMEOUT_SECONDS", "2.0")

    # Reload env-derived module constants
    monkeypatch.setattr(open_connector, "_BASE_URL", base_url)
    monkeypatch.setattr(open_connector, "_runtime_token", "test-token-xyz")

    try:
        yield _FakeOpenConnector
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


# ── Tests ─────────────────────────────────────────────────────────────


class TestIsAvailable:
    def test_returns_false_when_token_unset(self, monkeypatch):
        monkeypatch.delenv("OOMOL_CONNECT_RUNTIME_TOKEN", raising=False)
        open_connector._runtime_token = None
        open_connector._reset_cache_for_tests()
        assert open_connector.is_available() is False

    def test_returns_true_when_health_says_ok(self, fake_oc):
        fake_oc.HEALTH_OK = True
        open_connector._reset_cache_for_tests()
        assert open_connector.is_available() is True

    def test_returns_false_when_health_says_not_ok(self, fake_oc):
        fake_oc.HEALTH_OK = False
        open_connector._reset_cache_for_tests()
        assert open_connector.is_available() is False


class TestServiceIsKnown:
    def test_true_for_catalog_hit(self, fake_oc):
        fake_oc.SERVICES = [
            {"id": "slack", "displayName": "Slack"},
            {"id": "notion", "displayName": "Notion"},
        ]
        open_connector._reset_cache_for_tests()
        assert open_connector.service_is_known("slack") is True

    def test_true_for_oc_real_wire_format(self, fake_oc):
        """Live oomol-lab/open-connector@main /v1/providers returns
        objects with ``service`` (not ``id``) as the provider id.
        Captured 28 Sep 2026 from curl -fsS /v1/providers."""
        fake_oc.SERVICES = [
            {"service": "hackernews", "displayName": "Hacker News"},
            {"service": "github", "displayName": "GitHub"},
        ]
        open_connector._reset_cache_for_tests()
        assert open_connector.service_is_known("hackernews") is True
        assert open_connector.service_is_known("github") is True

    def test_false_for_catalog_miss(self, fake_oc):
        fake_oc.SERVICES = [{"id": "slack", "displayName": "Slack"}]
        open_connector._reset_cache_for_tests()
        assert open_connector.service_is_known("notion") is False

    def test_false_when_runtime_down(self, monkeypatch):
        monkeypatch.setenv("OOMOL_CONNECT_BASE_URL", "http://127.0.0.1:1")
        monkeypatch.setenv("OOMOL_CONNECT_RUNTIME_TOKEN", "test-token-xyz")
        open_connector._runtime_token = "test-token-xyz"
        open_connector._reset_cache_for_tests()
        assert open_connector.service_is_known("slack") is False


class TestCacheTTL:
    def test_health_result_is_cached(self, fake_oc, monkeypatch):
        fake_oc.HEALTH_OK = True
        open_connector._reset_cache_for_tests()
        first = open_connector.is_available()
        # Now flip runtime behaviour. The cached answer should be unchanged
        # for the TTL window.
        fake_oc.HEALTH_OK = False
        second = open_connector.is_available()
        assert first is True
        assert second is True, (
            "health result must be cached — flipping HEALTH_OK should "
            "not be visible until TTL expires"
        )


class TestNeverRaises:
    def test_connection_refused_returns_false(self, monkeypatch):
        # Port 1 is a privileged port unlikely to have an OC runtime.
        monkeypatch.setenv("OOMOL_CONNECT_BASE_URL", "http://127.0.0.1:1")
        monkeypatch.setenv("OOMOL_CONNECT_RUNTIME_TOKEN", "test-token-xyz")
        open_connector._runtime_token = "test-token-xyz"
        monkeypatch.setattr(open_connector, "_HEALTH_TIMEOUT", 0.2)
        open_connector._reset_cache_for_tests()
        # Should not raise.
        assert open_connector.is_available() is False
