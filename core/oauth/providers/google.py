import json
import base64
import urllib.parse
from typing import Dict, List
import aiohttp

from core.oauth.connector import BaseConnector

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v2/userinfo"
GOOGLE_REVOKE_URL = "https://oauth2.googleapis.com/revoke"


class GoogleConnector(BaseConnector):
    service = "google"
    name = "Google Workspace"
    icon = "G"
    scopes_read = [
        "email",
        "profile",
        "openid",
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/calendar.readonly",
        "https://www.googleapis.com/auth/spreadsheets.readonly",
    ]
    scopes_write = [
        "https://www.googleapis.com/auth/gmail.send",
        "https://www.googleapis.com/auth/calendar.events.create",
        "https://www.googleapis.com/auth/calendar",
        "https://www.googleapis.com/auth/sheets",
    ]
    supports_multi_account = True

    def _get_credentials(self) -> tuple:
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))
        from core.oauth.router import _load_oauth_settings
        settings = _load_oauth_settings()
        providers = settings.get("oauth", {}).get("providers", {})
        g = providers.get("google", {})
        return g.get("client_id", ""), g.get("client_secret", "")

    def authorize_url(self, state: str, redirect_uri: str) -> str:
        client_id, _ = self._get_credentials()
        scopes = " ".join(self.scopes_read + self.scopes_write)
        params = urllib.parse.urlencode({
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "scope": scopes,
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
            "include_granted_scopes": "true",
        })
        return f"{GOOGLE_AUTH_URL}?{params}"

    async def handle_callback(self, code: str, state: str, redirect_uri: str) -> dict:
        client_id, client_secret = self._get_credentials()
        payload = urllib.parse.urlencode({
            "code": code,
            "grant_type": "authorization_code",
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
        })
        async with aiohttp.ClientSession() as session:
            async with session.post(GOOGLE_TOKEN_URL, data=payload) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    raise Exception(f"Token exchange failed: {body}")
                data = await resp.json()

        user_info = await self.get_user_info(data)
        data["user_info"] = user_info
        return data

    async def test(self, token_data: dict) -> bool:
        access_token = token_data.get("access_token", "")
        if not access_token:
            return False
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    GOOGLE_USERINFO_URL,
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
        payload = urllib.parse.urlencode({
            "grant_type": "refresh_token",
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
        })
        async with aiohttp.ClientSession() as session:
            async with session.post(GOOGLE_TOKEN_URL, data=payload) as resp:
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
                    GOOGLE_USERINFO_URL,
                    headers={"Authorization": f"Bearer {access_token}"},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status == 200:
                        return await resp.json()
        except Exception:
            pass
        return {}

    def parse_error(self, status_code: int, body: dict) -> str:
        if status_code == 403:
            details = body.get("error_description", "") or body.get("error", "")
            if "admin_policy_enforced" in str(details):
                return (
                    "Your Google Workspace administrator has restricted third-party access. "
                    "Contact your admin to allow this application, or use a personal Gmail account."
                )
        if "unauthorized_client" in str(body):
            return (
                "This application is not authorized for your domain. "
                "Contact your Google Workspace admin to authorize it."
            )
        return f"Google authentication error (HTTP {status_code}): {body.get('error_description', body.get('error', ''))}"
