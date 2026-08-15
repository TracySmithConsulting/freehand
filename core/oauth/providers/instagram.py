import urllib.parse
from typing import Dict
import aiohttp

from core.oauth.connector import BaseConnector

META_AUTH_URL = "https://www.facebook.com/v19.0/dialog/oauth"
META_TOKEN_URL = "https://graph.facebook.com/v19.0/oauth/access_token"
META_IG_ME_URL = "https://graph.facebook.com/v19.0/me"


class InstagramConnector(BaseConnector):
    service = "instagram"
    name = "Instagram"
    icon = "I"
    scopes_read = [
        "instagram_basic",
        "pages_read_engagement",
    ]
    scopes_write = [
        "instagram_content_publish",
        "pages_manage_posts",
    ]
    supports_multi_account = True

    def _get_credentials(self) -> tuple:
        # OAuth broker Option B: Facebook and Instagram share Meta app creds.
        # We look up under "facebook" — broker returns app_id/secret pair
        # used for both. Falls back to user's meta.instagram settings.
        from core.oauth.broker import get_client_credentials
        app_id, app_secret, _source = get_client_credentials("facebook")
        if app_id and app_secret:
            return app_id, app_secret
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))
        from core.oauth.router import _load_oauth_settings
        settings = _load_oauth_settings()
        ig = settings.get("oauth", {}).get("providers", {}).get("meta", {}).get("instagram", {})
        return ig.get("app_id", ""), ig.get("app_secret", "")

    def authorize_url(self, state: str, redirect_uri: str) -> str:
        app_id, _ = self._get_credentials()
        scopes = ",".join(self.scopes_read + self.scopes_write)
        params = urllib.parse.urlencode({
            "client_id": app_id,
            "redirect_uri": redirect_uri,
            "scope": scopes,
            "state": state,
            "response_type": "code",
            "permission": "instagram_basic,instagram_content_publish,pages_read_engagement",
        })
        return f"{META_AUTH_URL}?{params}"

    async def handle_callback(self, code: str, state: str, redirect_uri: str) -> dict:
        app_id, app_secret = self._get_credentials()
        payload = urllib.parse.urlencode({
            "client_id": app_id,
            "client_secret": app_secret,
            "redirect_uri": redirect_uri,
            "code": code,
        })
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{META_TOKEN_URL}?{payload}") as resp:
                if resp.status != 200:
                    body = await resp.text()
                    raise Exception(f"Token exchange failed: {body}")
                data = await resp.json()
                return {"access_token": data.get("access_token", ""), "token_type": "Bearer"}

    async def test(self, token_data: dict) -> bool:
        access_token = token_data.get("access_token", "")
        if not access_token:
            return False
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{META_IG_ME_URL}?access_token={access_token}&fields=id,username",
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    return resp.status == 200
        except Exception:
            return False

    async def get_user_info(self, token_data: dict) -> dict:
        access_token = token_data.get("access_token", "")
        if not access_token:
            return {}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{META_IG_ME_URL}?access_token={access_token}&fields=id,username,account_type",
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status == 200:
                        return await resp.json()
        except Exception:
            pass
        return {}

    def parse_error(self, status_code: int, body: dict) -> str:
        return f"Instagram authentication error (HTTP {status_code}): {body.get('error', {}).get('message', str(body))}"
