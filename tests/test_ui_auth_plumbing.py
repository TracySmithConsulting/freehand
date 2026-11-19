"""Round 15.5 — browser auth plumbing invariants.

The console (freehand/static/index.html) previously shipped with ZERO key
wiring: every key-gated /api/* call 401'd, and the R15 approvals
EventSource was structurally unauthenticatable from a browser
(EventSource cannot send an X-API-Key header). The fix is client-side:
the key is stored in localStorage and attached to every /api/* call by a
single apiFetch shim, and the approvals stream is replaced by keyed
polling of GET /api/approvals. No new server surface.

/api/integrations/* used to be key-OPEN (on the idea that the provider's
consent screen is the authorisation). It isn't enough: the list, Disconnect
and Test answered anyone, so any web page could disconnect an account
(November 2026). Now they need the key; only the provider's callback is
open, and the Connect window gets a one-time ticket.

What this suite pins:
- static invariants on the shipped HTML (no bare fetch('/api/...') that
  would bypass the key, no headerless EventSource, localStorage key
  storage, remember/forget controls)
- the live auth BOUNDARY the browser relies on (TestClient): integrations,
  approvals + agent command key-gated; the OAuth callback open.

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
        # /api/console-key is an exception: it is how the console on this
        # computer gets the key in the first place (Nov 2026). So is
        # /api/sign-in/*: it's how someone without a key signs in as
        # themselves (core/sessions.py), and it needs no key.
        bare = re.findall(r"(?<![A-Za-z])fetch\(\s*'/api/(?!console-key'|sign-in/)", html)
        assert not bare, f"bare /api fetches bypass the key: {bare}"
        assert "fetch('/api/console-key'" in html

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

    def test_integrations_list_requires_key(self):
        c = TestClient(app)
        assert c.get("/api/integrations").status_code == 401
        assert c.post("/api/integrations/gmail/disconnect?label=work").status_code == 401
        assert c.post("/api/integrations/gmail/test").status_code == 401

    def test_integrations_list_works_with_key(self):
        c = TestClient(app, headers={"X-API-Key": get_or_create_api_key()})
        assert c.get("/api/integrations").status_code == 200

    def test_the_providers_callback_needs_no_key(self):
        # The provider sends the browser back here; its one-time state is checked.
        r = TestClient(app).get("/api/integrations/slack/callback?code=x&state=nonsense", follow_redirects=False)
        assert r.status_code not in (401, 403)

    def test_the_connect_window_needs_a_one_time_ticket(self):
        anyone = TestClient(app)
        url = "/api/integrations/nonesuch/authorize?label=work"
        assert anyone.get(url, follow_redirects=False).status_code == 401
        assert anyone.get(url + "&ticket=made-up", follow_redirects=False).status_code == 401
        assert anyone.post("/api/connect-ticket").status_code == 401
        owner = TestClient(app, headers={"X-API-Key": get_or_create_api_key()})
        ticket = owner.post("/api/connect-ticket").json()["ticket"]
        assert anyone.get(f"{url}&ticket={ticket}", follow_redirects=False).status_code != 401
        assert anyone.get(f"{url}&ticket={ticket}", follow_redirects=False).status_code == 401  # once only

    def test_a_member_cant_disconnect_the_owners_accounts(self):
        from core import agent_config, sessions
        from server import SESSION_HEADER
        s = agent_config._load_settings()
        s["people"] = [{"id": "thandi", "name": "Thandi", "role": "member"}]
        agent_config._save_settings(s)
        thandi = TestClient(app, headers={SESSION_HEADER: sessions.start("thandi")})
        assert thandi.post("/api/integrations/gmail/disconnect?label=work").status_code == 403
        # She may connect accounts of her own (tests/test_own_accounts.py),
        # but her ticket can't connect one named like the owner's.
        ticket = thandi.post("/api/connect-ticket").json()["ticket"]
        r = TestClient(app).get(f"/api/integrations/gmail/authorize?label=work&ticket={ticket}",
                                follow_redirects=False)
        assert r.status_code == 403

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
