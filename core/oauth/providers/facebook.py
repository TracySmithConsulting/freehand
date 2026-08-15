import urllib.parse
from typing import Dict
import aiohttp

from core.oauth.connector import BaseConnector

META_AUTH_URL = "https://www.facebook.com/v19.0/dialog/oauth"
META_TOKEN_URL = "https://graph.facebook.com/v19.0/oauth/access_token"
META_ME_URL = "https://graph.facebook.com/v19.0/me"


class FacebookConnector(BaseConnector):
    service = "facebook"
    name = "Facebook"
    icon = "f"
    scopes_read = [
        "pages_read_engagement",
        "email",
        "public_profile",
    ]
    scopes_write = [
        "pages_manage_posts",
        "pages_write_content",
    ]
    supports_multi_account = True

    def _get_credentials(self) -> tuple:
        # OAuth broker Option B: check user override, then env, then
        # broker_config.json. See core/oauth/broker.py.
        from core.oauth.broker import get_client_credentials
        # Facebook and Instagram share the Meta app_id/secret, so we
        # look up under "facebook" (broker key) which carries the
        # app_id/secret pair for both.
        app_id, app_secret, _source = get_client_credentials("facebook")
        if app_id and app_secret:
            return app_id, app_secret
        # Fallback: user's meta.facebook in settings.json
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))
        from core.oauth.router import _load_oauth_settings
        settings = _load_oauth_settings()
        fb = settings.get("oauth", {}).get("providers", {}).get("meta", {}).get("facebook", {})
        return fb.get("app_id", ""), fb.get("app_secret", "")

    def authorize_url(self, state: str, redirect_uri: str) -> str:
        app_id, _ = self._get_credentials()
        scopes = ",".join(self.scopes_read + self.scopes_write)
        params = urllib.parse.urlencode({
            "client_id": app_id,
            "redirect_uri": redirect_uri,
            "scope": scopes,
            "state": state,
            "response_type": "code",
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
                    f"{META_ME_URL}?access_token={access_token}&fields=id,name",
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
                    f"{META_ME_URL}?access_token={access_token}&fields=id,name,email",
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status == 200:
                        return await resp.json()
        except Exception:
            pass
        return {}

    def parse_error(self, status_code: int, body: dict) -> str:
        return f"Facebook authentication error (HTTP {status_code}): {body.get('error', {}).get('message', str(body))}"
