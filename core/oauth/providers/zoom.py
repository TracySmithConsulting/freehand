import urllib.parse
import base64
from typing import Dict
import aiohttp

from core.oauth.connector import BaseConnector

ZOOM_AUTH_URL = "https://zoom.us/oauth/authorize"
ZOOM_TOKEN_URL = "https://zoom.us/oauth/token"
ZOOM_REVOKE_URL = "https://zoom.us/oauth/revoke"
ZOOM_ME_URL = "https://api.zoom.us/v2/users/me"


class ZoomConnector(BaseConnector):
    service = "zoom"
    name = "Zoom"
    icon = "Z"
    scopes_read = [
        "meeting:read",
        "user:read",
        "user:read:list",
    ]
    scopes_write = [
        "meeting:write",
        "user:write",
    ]
    supports_multi_account = True

    def _get_credentials(self) -> tuple:
        # OAuth broker Option B: check user override, then env, then
        # broker_config.json. See core/oauth/broker.py.
        from core.oauth.broker import get_client_credentials
        client_id, client_secret, _source = get_client_credentials("zoom")
        if client_id and client_secret:
            return client_id, client_secret
        # Fallback: user's zoom in settings.json
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))
        from core.oauth.router import _load_oauth_settings
        settings = _load_oauth_settings()
        z = settings.get("oauth", {}).get("providers", {}).get("zoom", {})
        return z.get("client_id", ""), z.get("client_secret", "")

    def authorize_url(self, state: str, redirect_uri: str) -> str:
        client_id, _ = self._get_credentials()
        scopes = " ".join(self.scopes_read + self.scopes_write)
        params = urllib.parse.urlencode({
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": scopes,
            "state": state,
        })
        return f"{ZOOM_AUTH_URL}?{params}"

    async def handle_callback(self, code: str, state: str, redirect_uri: str) -> dict:
        client_id, client_secret = self._get_credentials()
        auth_header = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        payload = urllib.parse.urlencode({
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
        })
        headers = {"Authorization": f"Basic {auth_header}"}
        async with aiohttp.ClientSession() as session:
            async with session.post(ZOOM_TOKEN_URL, data=payload, headers=headers) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    raise Exception(f"Token exchange failed: {body}")
                return await resp.json()

    async def test(self, token_data: dict) -> bool:
        access_token = token_data.get("access_token", "")
        if not access_token:
            return False
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    ZOOM_ME_URL,
                    headers={"Authorization": f"Bearer {access_token}"},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    return resp.status == 200
        except Exception:
            return False

    async def refresh(self, token_data: dict) -> dict:
        client_id, client_secret = self._get_credentials()
        refresh_token = token_data.get("refresh_token", "")
        if not refresh_token:
            return None
        auth_header = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        payload = urllib.parse.urlencode({
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        })
        headers = {"Authorization": f"Basic {auth_header}"}
        async with aiohttp.ClientSession() as session:
            async with session.post(ZOOM_TOKEN_URL, data=payload, headers=headers) as resp:
                if resp.status != 200:
                    return None
                return await resp.json()

    async def get_user_info(self, token_data: dict) -> dict:
        access_token = token_data.get("access_token", "")
        if not access_token:
            return {}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    ZOOM_ME_URL,
                    headers={"Authorization": f"Bearer {access_token}"},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status == 200:
                        return await resp.json()
        except Exception:
            pass
        return {}

    def parse_error(self, status_code: int, body: dict) -> str:
        return f"Zoom authentication error (HTTP {status_code}): {body.get('error_description', body.get('error', ''))}"
