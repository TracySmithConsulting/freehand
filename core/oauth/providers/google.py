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
        # calendar.events.create is a granular sub-scope of calendar; Google
        # rejects it as a standalone scope on the consent screen, but it's
        # already implied by the broader "calendar" scope below.
        "https://www.googleapis.com/auth/calendar",
        # Same story: "sheets" is a granular sub-scope of "spreadsheets".
        # We request the broader one and Google grants everything.
        "https://www.googleapis.com/auth/spreadsheets",
    ]
    supports_multi_account = True

    def _get_credentials(self) -> tuple:
        # OAuth broker Option B: check user override, then env, then
        # broker_config.json. See core/oauth/broker.py.
        from core.oauth.broker import get_client_credentials
        client_id, client_secret, _source = get_client_credentials("google")
        return client_id or "", client_secret or ""

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
        # Token exchange must be application/x-www-form-urlencoded with all
        # special characters properly escaped. Passing a dict to aiohttp's
        # `data=` kwarg gets this right automatically (including URL-encoding
        # the slashes in OAuth codes as %2F). Passing a pre-urlencoded string
        # makes aiohttp default to text/plain — which Google's token endpoint
        # rejects with "Invalid JSON payload received. Unexpected token."
        payload_dict = {
            "code": code,
            "grant_type": "authorization_code",
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(GOOGLE_TOKEN_URL, data=payload_dict) as resp:
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
