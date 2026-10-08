"""Round 15.5 — browser auth plumbing invariants.

The console (freehand/static/index.html) previously shipped with ZERO key
wiring: every key-gated /api/* call 401'd, and the R15 approvals
EventSource was structurally unauthenticatable from a browser
(EventSource cannot send an X-API-Key header). The fix is client-side:
the key is stored in localStorage and attached to every /api/* call by a
single apiFetch shim, and the approvals stream is replaced by keyed
polling of GET /api/approvals. No new server surface.

Connector setup stays reachable before any key is entered:
/api/integrations/* is key-OPEN by design (the provider's OAuth consent
screen IS the authorisation), so "connect a service in the browser" works
with just the connector lifecycle.

What this suite pins:
- static invariants on the shipped HTML (no bare fetch('/api/...') that
  would bypass the key, no headerless EventSource, localStorage key
  storage, remember/forget controls)
- the live auth BOUNDARY the browser relies on (TestClient): integrations
  open, approvals + agent command key-gated.

No new runtime deps.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server import app, get_or_create_api_key  # noqa: E402

INDEX = Path(__file__).resolve().parent.parent / "freehand" / "static" / "index.html"


def _html() -> str:
    return INDEX.read_text(encoding="utf-8")


class TestKeyPlumbing:
    def test_key_is_stored_client_side(self):
        html = _html()
        assert "freehand_api_key" in html, "no localStorage key constant"
        assert "localStorage" in html

    def test_api_fetch_shim_exists_and_attaches_key(self):
        html = _html()
        m = re.search(r"function apiFetch\(", html)
        assert m, "apiFetch shim is missing from index.html"
        body = html[m.end(): m.end() + 1500]
        assert "X-API-Key" in body, "shim does not attach X-API-Key"
        assert "freehand_api_key" in body or "storedApiKey" in body

    def test_no_bare_api_fetches_bypass_the_shim(self):
        """Any bare fetch('/api/...') would 401 again for a user who has
        stored a key — every /api/* call must go through apiFetch."""
        html = _html()
        bare = re.findall(r"(?<![A-Za-z])fetch\(\s*'/api/", html)
        assert not bare, f"bare /api fetches bypass the key: {bare}"

    def test_headerless_event_source_channel_is_gone(self):
        """EventSource cannot send an X-API-Key header; approvals must be
        polled (keyed) instead of EventSource'd."""
        html = _html()
        assert "new EventSource(" not in html, "headerless EventSource still wired"
        assert "setInterval(loadApprovals" in html, "keyed approvals polling missing"

    def test_key_hint_and_controls_exist(self):
        html = _html()
        assert "remember-key-btn" in html
        assert "forget-key-btn" in html
        assert "settings.json" in html, "no hint telling the user where the key lives"


class TestConnectorBoundary:
    """Live boundary the browser relies on. A future auth change that
    closes /api/integrations or opens /api/approvals would silently break
    'set up a connector from the browser' — this pins it."""

    def test_integrations_list_is_open_no_key(self):
        c = TestClient(app)
        assert c.get("/api/integrations").status_code == 200

    def test_approvals_list_requires_key(self):
        c = TestClient(app)
        assert c.get("/api/approvals").status_code == 401

    def test_approvals_list_works_with_key(self):
        c = TestClient(app, headers={"X-API-Key": get_or_create_api_key()})
        assert c.get("/api/approvals").status_code == 200

    def test_agent_command_requires_key(self):
        c = TestClient(app)
        r = c.post("/api/agent/command", json={"command": "x", "source": "web"})
        assert r.status_code == 401
