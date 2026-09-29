import urllib.parse
from typing import Dict
import aiohttp

from core.oauth.connector import BaseConnector

MS_AUTH_URL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize"
MS_TOKEN_URL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
MS_ME_URL = "https://graph.microsoft.com/v1.0/me"
MS_REVOKE_URL = "https://graph.microsoft.com/v1.0/me/refreshToken"


class MicrosoftConnector(BaseConnector):
    service = "microsoft"
    name = "Microsoft 365"
    icon = "M"
    scopes_read = [
        "User.Read",
        "Mail.Read",
        "Calendars.Read",
        "Contacts.Read",
        "offline_access",
    ]
    scopes_write = [
        "Mail.Send",
        "Calendars.ReadWrite",
        "Tasks.ReadWrite",
        "Mail.ReadWrite",
    ]
    supports_multi_account = True

    def _get_credentials(self) -> tuple:
        # OAuth broker Option B: client_id/secret come from broker.
        # Tenant stays in user settings (it's not a secret — "common" by default).
        from core.oauth.broker import get_client_credentials
        client_id, client_secret, _source = get_client_credentials("microsoft")
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))
        from core.oauth.router import _load_oauth_settings
        settings = _load_oauth_settings()
        m = settings.get("oauth", {}).get("providers", {}).get("microsoft", {})
        tenant = m.get("tenant", "common")
        return client_id or "", client_secret or "", tenant

    def authorize_url(self, state: str, redirect_uri: str) -> str:
        client_id, client_secret, tenant = self._get_credentials()
        scopes = " ".join(self.scopes_read + self.scopes_write)
        auth_url = MS_AUTH_URL.format(tenant=tenant)
        params = urllib.parse.urlencode({
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "scope": scopes,
            "response_mode": "query",
            "state": state,
            "prompt": "consent",
        })
        return f"{auth_url}?{params}"

    async def handle_callback(self, code: str, state: str, redirect_uri: str) -> dict:
        client_id, client_secret, tenant = self._get_credentials()
        token_url = MS_TOKEN_URL.format(tenant=tenant)
        # Pass dict (not pre-urlencoded string) so aiohttp sets
        # Content-Type: application/x-www-form-urlencoded. Passing a string
        # makes aiohttp send text/plain, which Microsoft rejects with
        # AADSTS900144 "request body must contain grant_type". Same bug
        # pattern as Google on round 5 (commits 63b32fa).
        payload_dict = {
            "code": code,
            "grant_type": "authorization_code",
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "scope": " ".join(self.scopes_read + self.scopes_write),
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(token_url, data=payload_dict) as resp:
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
                    MS_ME_URL,
                    headers={"Authorization": f"Bearer {access_token}"},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    return resp.status == 200
        except Exception:
            return False

    async def refresh(self, token_data: dict) -> dict:
        client_id, client_secret, tenant = self._get_credentials()
        refresh_token = token_data.get("refresh_token", "")
        if not refresh_token:
            return None
        token_url = MS_TOKEN_URL.format(tenant=tenant)
        # Pass dict (not pre-encoded string) so aiohttp sets
        # Content-Type: application/x-www-form-urlencoded automatically.
        # Passing a string as data= causes aiohttp to send it as text/plain,
        # which Microsoft rejects with AADSTS900144 "request body must contain
        # grant_type". Same fix as handle_callback (round 5, commit 63b32fa).
        payload = {
            "grant_type": "refresh_token",
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(token_url, data=payload) as resp:
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
                    MS_ME_URL,
                    headers={"Authorization": f"Bearer {access_token}"},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status == 200:
                        return await resp.json()
        except Exception:
            pass
        return {}

    def parse_error(self, status_code: int, body: dict) -> str:
        err = body.get("error", "")
        desc = body.get("error_description", "") or str(body.get("error_uri", ""))
        # AADSTS900144: request body missing grant_type — almost always means
        # aiohttp sent the body as text/plain instead of form-encoded.
        if "AADSTS900144" in str(body):
            return (
                "Token exchange failed: Microsoft rejected the request body format "
                "(AADSTS900144). This is an internal bug — the code was not sending "
                "the credentials in the correct format. Please reconnect your account."
            )
        if "admin_policy_enforced" in str(desc) or "consent_required" in str(desc):
            return (
                "Your Microsoft 365 administrator has restricted third-party access. "
                "Contact your admin to grant consent, or use a personal Microsoft account."
            )
        if "unauthorized_client" in str(err):
            return "Application is not authorized for your tenant. Contact your admin."
        return f"Microsoft authentication error (HTTP {status_code}): {desc or err}"
