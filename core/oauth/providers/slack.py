"""Slack OAuth connector (Round 9).

Closes the tier-1b-for-new-services gap flagged in skill Pitfall 39:
tier-1b broker resolution works end-to-end only for services with a
FreeHand connector. With this in place, ``freehand shared-app add
slack --client-id ... --client-secret ... --scopes ...`` actually
completes the OAuth dance through FreeHand — no OC needed at runtime
for Slack.

Public surface mirrors the existing Google / Microsoft / Zoom
connectors (see ``core/oauth/providers/google.py`` for the canonical
shape):

- ``_get_credentials()`` — broker lookup via ``get_client_credentials``
  so tier-1b shared apps work transparently
- ``authorize_url(state, redirect_uri)`` — builds the Slack consent URL
  with deduplicated scopes
- ``handle_callback(code, state, redirect_uri)`` — POSTs to
  ``oauth.v2.user.access`` with a dict body (Pitfall 16) and surfaces
  Slack's ``{"ok": false, "error": "..."}`` error responses explicitly
- ``test(token_data)`` — calls ``auth.test`` to verify the token

Wire-format shapes (Pitfall 30 — captured live from the Round 8 smoke):

  Slack POST /api/oauth.v2.user.access success:
    {"ok": true, "access_token": "xoxb-...", "scope": "...",
     "team": {"id": "T...", "name": "..."}, "bot_user_id": "U..."}

  Slack POST /api/oauth.v2.user.access failure:
    {"ok": false, "error": "invalid_code"}

  Slack POST /api/auth.test success:
    {"ok": true, "url": "https://team.slack.com/", "team": "...",
     "user": "...", "team_id": "T...", "user_id": "U..."}

Scope handling — Tracy's decision (01 Oct 2026): include ``im:write``
in the default ``scopes_write`` set. Document the privacy implication
("the bot can DM anyone the workspace allows") and provide the
opt-out recipe in ``docs/integrations/slack-oauth-setup.md``.

Connector does NOT implement ``refresh()`` — Slack user/bot tokens
don't expire (until explicitly revoked via ``auth.revoke``). Refreshing
is therefore not a concern for this connector; if a stored token
stops working, ``auth.test`` returns ok=false and the user reconnects.

Token type note: the OAuth dance returns a Bot User OAuth Token
(``xoxb-...``) when the user installs the app with the requested Bot
Token Scopes. The agent acts AS THE BOT, not as the installing user.
This is the right default for an agent and matches the Round 8 smoke
result (``accountId: U0C56R6LTD4`` was the bot user ID, not Tracy's
personal Slack user ID).
"""

import urllib.parse
from typing import Dict, List

import aiohttp

from core.oauth.connector import BaseConnector

SLACK_AUTHORIZE_URL = "https://slack.com/oauth/v2_user/authorize"
SLACK_TOKEN_URL = "https://slack.com/api/oauth.v2.user.access"
SLACK_AUTH_TEST_URL = "https://slack.com/api/auth.test"


class SlackConnector(BaseConnector):
    service = "slack"
    name = "Slack"
    icon = "S"
    # Read-only scopes: list channels, look up users, read DM history.
    scopes_read: List[str] = ["channels:read", "users:read", "im:history"]
    # Write scopes: post messages to channels, send DMs.
    # im:write is included per Tracy's decision (01 Oct 2026) — the bot
    # can DM any user the workspace allows. Opt-out recipe: register the
    # Slack app without im:write in Bot Token Scopes, then
    # `freehand shared-app add slack --scopes "chat:write,channels:read,users:read,im:history"`.
    scopes_write: List[str] = ["chat:write", "im:write"]
    supports_multi_account = True

    def _get_credentials(self) -> tuple:
        """Read Slack credentials via the broker.

        Mirrors ``GoogleConnector._get_credentials()``. Picks up tier-1b
        shared apps transparently because the broker is the single
        source of truth.
        """
        from core.oauth.broker import get_client_credentials
        client_id, client_secret, _source = get_client_credentials("slack")
        return client_id or "", client_secret or ""

    def authorize_url(self, state: str, redirect_uri: str) -> str:
        """Build the Slack OAuth authorize URL.

        Scopes are deduplicated — Slack rejects duplicate scopes with
        ``invalid_scope`` if the same scope appears in both
        ``scopes_read`` and ``scopes_write`` (Pitfall 30).
        """
        client_id, _ = self._get_credentials()
        # Deduplicate while preserving order. dict.fromkeys() does this.
        all_scopes = list(dict.fromkeys(self.scopes_read + self.scopes_write))
        # Slack uses a single space as the scope separator (URL-encoded
        # to %20 or + by urlencode).
        scope_str = " ".join(all_scopes)
        params = urllib.parse.urlencode({
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "scope": scope_str,
            "state": state,
        })
        return f"{SLACK_AUTHORIZE_URL}?{params}"

    async def handle_callback(self, code: str, state: str, redirect_uri: str) -> dict:
        """Exchange the authorization code for a Bot User OAuth Token.

        Returns a dict shaped for ``core.oauth.manager.save_connection``
        to persist in ``vault/connections.db``.
        """
        client_id, client_secret = self._get_credentials()
        # Pass a DICT to aiohttp's data= kwarg. aiohttp sets Content-Type
        # to application/x-www-form-urlencoded automatically. Passing a
        # pre-urlencoded string would default to text/plain, which Slack
        # rejects (Pitfall 16 — the same bug pattern that hit Google and
        # Microsoft).
        payload_dict = {
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "redirect_uri": redirect_uri,
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(SLACK_TOKEN_URL, data=payload_dict) as resp:
                payload = await resp.json()

        # Slack returns {"ok": false, "error": "..."} on failure. Surface
        # the error string rather than letting a missing access_token
        # fail silently downstream.
        if not payload.get("ok"):
            error = payload.get("error", "unknown")
            raise RuntimeError(f"Slack token exchange failed: {error}")

        team = payload.get("team", {}) or {}
        return {
            "access_token": payload["access_token"],
            "scope": payload.get("scope", ""),
            "team_id": team.get("id"),
            "team_name": team.get("name"),
            "bot_user_id": payload.get("bot_user_id"),
            # Slack user/bot tokens don't expire (until explicitly revoked).
            # Set expires_in to a far-future value so the manager doesn't
            # treat the token as expired on first save.
            "expires_in": None,
        }

    async def test(self, token_data: dict) -> bool:
        """Verify a stored Slack connection still works.

        Calls Slack's ``auth.test`` endpoint with the stored access_token.
        Returns True iff Slack responds ``{"ok": true}``.
        """
        access_token = token_data.get("access_token", "")
        if not access_token:
            return False
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    SLACK_AUTH_TEST_URL,
                    headers={"Authorization": f"Bearer {access_token}"},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    payload = await resp.json()
                    return bool(payload.get("ok"))
        except Exception:
            return False

    def parse_error(self, status_code: int, body: dict) -> str:
        """Human-readable Slack OAuth error message.

        Slack's error responses are JSON objects with ``ok: false`` and
        an ``error`` field. Some common error codes:
        - ``invalid_code``: code expired, malformed, or already used
        - ``invalid_client_id``: client_id not recognised
        - ``bad_client_secret``: client_secret doesn't match
        - ``redirect_uri_does_not_match_app_config``: redirect URI not
          registered in the Slack app (Pitfall 33 — usually means the
          app needs a Reinstall)
        """
        if isinstance(body, dict) and body.get("ok") is False:
            error = body.get("error", "")
            hint = ""
            if error == "redirect_uri_does_not_match_app_config":
                hint = (
                    " (the Slack app probably needs a Reinstall to Workspace "
                    "for the new Redirect URL to take effect — see Pitfall 33)"
                )
            elif error == "invalid_code":
                hint = " (the code may have expired — try the dance again)"
            return f"Slack authentication error: {error}{hint}"
        return f"Slack authentication error (HTTP {status_code}): {body}"
