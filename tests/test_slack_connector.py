"""Tests for the Slack OAuth connector (Round 9).

Round 9 of FreeHand maintenance.

Closes the tier-1b gap flagged in the skill's Pitfall 39: tier-1b
broker resolution works end-to-end only for services that have a
FreeHand connector in core/oauth/providers/__init__.py. The Slack
connector is the first new entry added since the initial seven.

Covers:
- authorize_url: builds Slack OAuth URL with client_id, redirect_uri,
  scope, state. URL-encodes the scope parameter (Slack rejects
  un-encoded spaces). Deduplicates scopes (Slack rejects duplicates).
- handle_callback: POSTs to slack.com/api/oauth.v2.user.access with
  dict body (Pitfall 16 — aiohttp Content-Type trap). Surfaces Slack's
  {"ok": false, "error": "..."} response explicitly rather than
  swallowing it. Returns the parsed token_data shape that the manager
  stores in vault/connections.db.
- test(token_data): calls auth.test, returns True iff Slack responds
  ok=true.
- Broker integration: connector's _get_credentials() reads through
  broker.get_client_credentials("slack") — picks up tier-1b shared
  app transparently.
- Registry integration: get_connector("slack") returns a SlackConnector
  instance after core/oauth/providers/__init__.py registration.
"""

from __future__ import annotations

import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, r"C:\Users\trace\Documents\Default Project")

from core.oauth import broker  # noqa: E402
from core.oauth.providers import get_connector  # noqa: E402


# ── Connector registry ─────────────────────────────────────────────────


class TestSlackConnectorRegistry:
    def test_get_connector_returns_slack_connector_instance(self):
        """get_connector("slack") must return a SlackConnector instance.

        Before Task 2 lands, this fails with None (slack not in CONNECTORS).
        After Task 2 lands, it returns a SlackConnector with the class
        attributes defined below.
        """
        connector = get_connector("slack")
        assert connector is not None, "get_connector('slack') returned None"
        # Sanity-check the class identity without importing the module
        # directly (so this test still works mid-slice).
        assert connector.__class__.__name__ == "SlackConnector"

    def test_slack_connector_class_metadata(self):
        """Class-level metadata the router/UI depends on."""
        connector = get_connector("slack")
        assert connector.name == "Slack"
        assert connector.supports_multi_account is True
        # Scopes match the Round 9 plan's locked set (Tracy 01 Oct 2026).
        assert connector.scopes_read == ["channels:read", "users:read", "im:history"]
        assert connector.scopes_write == ["chat:write", "im:write"]


# ── authorize_url ───────────────────────────────────────────────────────


class TestSlackAuthorizeUrl:
    def test_authorize_url_uses_slack_endpoint(self):
        """authorize_url must target slack.com/oauth/v2_user/authorize."""
        connector = get_connector("slack")
        # Stub out credentials so the URL builds without real config
        with patch.object(
            broker, "get_client_credentials",
            return_value=("client_abc", "secret_xyz", "shared_app"),
        ):
            url = connector.authorize_url(
                state="test_state",
                redirect_uri="http://localhost:8000/api/integrations/slack/callback",
            )
        assert url.startswith("https://slack.com/oauth/v2_user/authorize?")

    def test_authorize_url_includes_required_params(self):
        """client_id, redirect_uri, response_type=code, scope, state all present."""
        connector = get_connector("slack")
        with patch.object(
            broker, "get_client_credentials",
            return_value=("client_abc", "secret_xyz", "shared_app"),
        ):
            url = connector.authorize_url(
                state="abc_state",
                redirect_uri="http://localhost:8000/api/integrations/slack/callback",
            )
        assert "client_id=client_abc" in url
        assert "response_type=code" in url
        assert "redirect_uri=" in url
        # redirect_uri must be URL-encoded (Pitfall 11 — colons and slashes)
        assert "http%3A%2F%2F" in url
        assert "scope=" in url
        assert "state=abc_state" in url

    def test_authorize_url_includes_all_default_scopes(self):
        """All five scopes (3 read + 2 write) appear in the URL."""
        connector = get_connector("slack")
        with patch.object(
            broker, "get_client_credentials",
            return_value=("client_abc", "secret_xyz", "shared_app"),
        ):
            url = connector.authorize_url(
                state="s",
                redirect_uri="http://localhost:8000/api/integrations/slack/callback",
            )
        # Slack uses %20 for spaces in scopes
        assert "channels%3Aread" in url or "channels:read" in url
        assert "users%3Aread" in url or "users:read" in url
        assert "im%3Ahistory" in url or "im:history" in url
        assert "chat%3Awrite" in url or "chat:write" in url
        assert "im%3Awrite" in url or "im:write" in url

    def test_authorize_url_deduplicates_scopes(self):
        """If a scope appears in both scopes_read and scopes_write, it must
        only appear once. Slack rejects duplicate scopes with 'invalid_scope'."""
        connector = get_connector("slack")
        # Force a duplicate by monkey-patching scopes on the instance
        with patch.object(
            connector, "scopes_read", ["chat:write", "channels:read"]
        ), patch.object(
            connector, "scopes_write", ["chat:write", "im:write"]
        ), patch.object(
            broker, "get_client_credentials",
            return_value=("cid", "sec", "shared_app"),
        ):
            url = connector.authorize_url(
                state="s",
                redirect_uri="http://localhost:8000/api/integrations/slack/callback",
            )
        # chat:write should appear EXACTLY ONCE in the scope param
        # (counting URL-encoded form)
        scope_part = url.split("scope=", 1)[1].split("&", 1)[0]
        # decode and count
        from urllib.parse import unquote
        decoded = unquote(scope_part)
        assert decoded.count("chat:write") == 1, (
            f"chat:write should appear once in scope, got: {decoded!r}"
        )


# ── handle_callback ─────────────────────────────────────────────────────


class TestSlackHandleCallback:
    """Pinned against the live Slack wire shape captured in Round 8 smoke.

    Slack POST /api/oauth.v2.user.access returns:
      success: {"ok": true, "access_token": "xoxb-...", "scope": "...",
                "team": {"id": "...", "name": "..."}, "bot_user_id": "..."}
      failure: {"ok": false, "error": "invalid_code"}
    """

    @pytest.mark.asyncio
    async def test_handle_callback_success_returns_token_data(self):
        """Success path: parse the Slack token response and return the
        fields the manager needs to store in vault/connections.db."""
        connector = get_connector("slack")
        # Live wire shape — pinned per Pitfall 30
        slack_response = {
            "ok": True,
            "access_token": "xoxb-test-token-12345",
            "scope": "chat:write,channels:read,users:read,im:history,im:write",
            "team": {"id": "T12345", "name": "Test Workspace"},
            "bot_user_id": "U67890",
            "enterprise": None,
        }

        # Mock aiohttp.ClientSession.post to return our pinned fixture
        mock_resp = AsyncMock()
        mock_resp.json = AsyncMock(return_value=slack_response)
        # Async context manager protocol
        mock_post = MagicMock()
        mock_post.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_post.__aexit__ = AsyncMock(return_value=None)

        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=mock_post)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)

        with patch.object(
            broker, "get_client_credentials",
            return_value=("cid", "sec", "shared_app"),
        ), patch(
            "aiohttp.ClientSession", return_value=mock_session
        ):
            result = await connector.handle_callback(
                code="auth_code_from_slack",
                state="test_state",
                redirect_uri="http://localhost:8000/api/integrations/slack/callback",
            )

        assert result["access_token"] == "xoxb-test-token-12345"
        assert result["scope"] == slack_response["scope"]
        assert result["team_id"] == "T12345"
        assert result["team_name"] == "Test Workspace"
        assert result["bot_user_id"] == "U67890"

    @pytest.mark.asyncio
    async def test_handle_callback_uses_dict_body_not_pre_encoded_string(self):
        """Pitfall 16 — aiohttp `data=dict` sets Content-Type
        application/x-www-form-urlencoded. `data=str` defaults to
        text/plain which Slack rejects. The connector must use dict."""
        connector = get_connector("slack")
        mock_resp = AsyncMock()
        mock_resp.json = AsyncMock(return_value={
            "ok": True, "access_token": "xoxb-test", "scope": "",
            "team": {"id": "T", "name": "W"}, "bot_user_id": "U",
        })
        mock_post = MagicMock()
        mock_post.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_post.__aexit__ = AsyncMock(return_value=None)
        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=mock_post)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)

        with patch.object(
            broker, "get_client_credentials",
            return_value=("cid", "sec", "shared_app"),
        ), patch(
            "aiohttp.ClientSession", return_value=mock_session
        ):
            await connector.handle_callback(
                code="c", state="s",
                redirect_uri="http://localhost:8000/api/integrations/slack/callback",
            )

        # Inspect what was passed to session.post(... data=...)
        # The positional/keyword argument named 'data' must be a dict, not a str.
        call_kwargs = mock_session.post.call_args.kwargs
        assert "data" in call_kwargs, "session.post() must use data= kwarg"
        assert isinstance(call_kwargs["data"], dict), (
            f"data must be dict (Pitfall 16), got {type(call_kwargs['data']).__name__}"
        )
        assert call_kwargs["data"]["code"] == "c"
        assert call_kwargs["data"]["client_id"] == "cid"
        assert call_kwargs["data"]["client_secret"] == "sec"
        assert call_kwargs["data"]["redirect_uri"] == (
            "http://localhost:8000/api/integrations/slack/callback"
        )

    @pytest.mark.asyncio
    async def test_handle_callback_failure_surfaces_slack_error(self):
        """Slack returns {"ok": false, "error": "invalid_code"} on failure.
        The connector must raise with the error visible — NOT silently
        return a dict with no access_token."""
        connector = get_connector("slack")
        mock_resp = AsyncMock()
        mock_resp.json = AsyncMock(return_value={
            "ok": False,
            "error": "invalid_code",
            "warning": "missing_charset",
            "response_metadata": {"warnings": ["missing_charset"]},
        })
        mock_post = MagicMock()
        mock_post.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_post.__aexit__ = AsyncMock(return_value=None)
        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=mock_post)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)

        with patch.object(
            broker, "get_client_credentials",
            return_value=("cid", "sec", "shared_app"),
        ), patch(
            "aiohttp.ClientSession", return_value=mock_session
        ):
            with pytest.raises(RuntimeError) as exc_info:
                await connector.handle_callback(
                    code="bad_code", state="s",
                    redirect_uri="http://localhost:8000/api/integrations/slack/callback",
                )
        # The Slack error string must appear in the exception message
        assert "invalid_code" in str(exc_info.value), (
            f"Slack error must surface in exception: {exc_info.value}"
        )


# ── test() ──────────────────────────────────────────────────────────────


class TestSlackTest:
    """test(token_data) verifies a stored Slack connection still works."""

    @pytest.mark.asyncio
    async def test_returns_true_when_slack_responds_ok(self):
        connector = get_connector("slack")
        mock_resp = AsyncMock()
        mock_resp.json = AsyncMock(return_value={
            "ok": True,
            "url": "https://test.slack.com/",
            "team": "Test Workspace",
            "user": "test_user",
            "team_id": "T12345",
            "user_id": "U12345",
        })
        mock_post = MagicMock()
        mock_post.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_post.__aexit__ = AsyncMock(return_value=None)
        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=mock_post)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            ok = await connector.test({"access_token": "xoxb-stored-token"})
        assert ok is True

    @pytest.mark.asyncio
    async def test_returns_false_when_slack_responds_not_ok(self):
        connector = get_connector("slack")
        mock_resp = AsyncMock()
        mock_resp.json = AsyncMock(return_value={
            "ok": False,
            "error": "invalid_auth",
        })
        mock_post = MagicMock()
        mock_post.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_post.__aexit__ = AsyncMock(return_value=None)
        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=mock_post)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            ok = await connector.test({"access_token": "xoxb-revoked"})
        assert ok is False

    @pytest.mark.asyncio
    async def test_returns_false_when_token_data_missing_access_token(self):
        connector = get_connector("slack")
        ok = await connector.test({})
        assert ok is False


# ── Broker tier-1b integration ──────────────────────────────────────────


class TestSlackConnectorBrokerIntegration:
    """The connector reads credentials via broker.get_client_credentials(),
    so tier-1b shared apps work transparently — same pattern as the
    Google connector's _get_credentials() helper."""

    def test_get_credentials_picks_up_tier1b_shared_app(self):
        """With a tier-1b shared app registered, _get_credentials returns
        the FreeHand-managed client_id + client_secret (NOT None, NOT
        a pointer like tier-5 OC)."""
        connector = get_connector("slack")
        with patch.object(
            broker, "_load_shared_apps",
            return_value={
                "slack": {
                    "client_id": "shared_id",
                    "client_secret": "shared_secret",
                },
            },
        ):
            cid, sec = connector._get_credentials()
        assert cid == "shared_id"
        assert sec == "shared_secret"

    def test_get_credentials_empty_when_no_creds_anywhere(self):
        """Without any tier set, _get_credentials returns empty strings
        (NOT an exception) — same fallback shape as the Google connector."""
        connector = get_connector("slack")
        with patch.object(broker, "_load_user_settings", return_value={}), \
             patch.object(broker, "_load_shared_apps", return_value={}), \
             patch.object(broker, "_oc_available", return_value=False):
            cid, sec = connector._get_credentials()
        assert cid == ""
        assert sec == ""
