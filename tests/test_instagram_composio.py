"""Instagram runs on Composio (Nov 2026): OpenConnector would need the
user's own Meta developer app, which won't take a localhost address."""
import asyncio

from core.oauth import composio
from core.tools import integrations


def _account(monkeypatch, present=True):
    acct = {"connected_account_id": "ca_1", "user_id": "u1", "label": "default"} if present else None
    monkeypatch.setattr(composio, "get_account", lambda service, label="default": acct)


def test_listing_uses_the_composio_connection(monkeypatch):
    _account(monkeypatch)
    calls = []

    async def fake_exec(slug, account_id, args=None, user_id=""):
        calls.append((slug, account_id, args, user_id))
        return {"successful": True, "data": {"id": "17841", "username": "tracy"}}
    monkeypatch.setattr(composio, "execute_tool", fake_exec)
    out = asyncio.run(integrations.list_instagram_accounts())
    assert out == [{"id": "17841", "username": "tracy"}]
    assert calls == [("INSTAGRAM_GET_USER_INFO", "ca_1", {}, "u1")]


def test_posting_uploads_then_publishes(monkeypatch):
    _account(monkeypatch)
    monkeypatch.setattr(integrations, "_check_write_permission", lambda *a, **k: {"allowed": True})
    calls = []
    replies = {"INSTAGRAM_GET_USER_INFO": {"id": "17841"},
               "INSTAGRAM_CREATE_MEDIA_CONTAINER": {"id": "c99"},
               "INSTAGRAM_POST_IG_USER_MEDIA_PUBLISH": {"id": "m42"}}

    async def fake_exec(slug, account_id, args=None, user_id=""):
        calls.append((slug, args))
        return {"successful": True, "data": replies[slug]}
    monkeypatch.setattr(composio, "execute_tool", fake_exec)
    out = asyncio.run(integrations.post_to_instagram(content="Hello", media_url="https://x.test/a.jpg"))
    assert out == {"id": "m42"}
    assert calls[1] == ("INSTAGRAM_CREATE_MEDIA_CONTAINER",
                        {"ig_user_id": "17841", "image_url": "https://x.test/a.jpg", "caption": "Hello"})
    assert calls[2] == ("INSTAGRAM_POST_IG_USER_MEDIA_PUBLISH", {"ig_user_id": "17841", "creation_id": "c99"})


def test_not_connected_points_to_connections(monkeypatch):
    _account(monkeypatch, present=False)
    out = asyncio.run(integrations.list_instagram_accounts())
    assert "Connections" in out["error"] and out["connect_url"].startswith("/api/integrations/instagram/authorize")


def test_instagram_is_no_longer_an_openconnector_app():
    import importlib
    router = importlib.import_module("core.oauth.router")
    assert "instagram" not in router.OC_FEATURED_SERVICES
    assert ("instagram", "posts") not in integrations.OC_APPS
