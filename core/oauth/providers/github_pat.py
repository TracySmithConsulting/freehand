import urllib.parse
from typing import Dict
import aiohttp

from core.oauth.connector import BaseConnector

GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"
GITHUB_USER_URL = "https://api.github.com/user"


class GitHubPATConnector(BaseConnector):
    service = "github"
    name = "GitHub"
    icon = "GH"
    scopes_read = [
        "repo",
        "gist",
        "read:org",
    ]
    scopes_write = [
        "repo",
        "gist",
        "workflow",
    ]
    supports_multi_account = True
    uses_pat = True

    def authorize_url(self, state: str, redirect_uri: str) -> str:
        return (
            "https://github.com/login/oauth/authorize?"
            + urllib.parse.urlencode({
                "client_id": "placeholder",
                "redirect_uri": redirect_uri,
                "state": state,
                "scope": " ".join(self.scopes_read + self.scopes_write),
            })
        )

    async def handle_callback(self, code: str, state: str, redirect_uri: str) -> dict:
        return {"token_type": "pat", "requires_pat": True}

    async def validate_pat(self, pat: str) -> dict:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    GITHUB_USER_URL,
                    headers={
                        "Authorization": f"token {pat}",
                        "Accept": "application/vnd.github.v3+json",
                    },
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status != 200:
                        body = await resp.json()
                        raise Exception(body.get("message", f"HTTP {resp.status}"))
                    user = await resp.json()
                    return {
                        "access_token": pat,
                        "token_type": "pat",
                        "user": user,
                        "login": user.get("login", ""),
                    }
        except Exception as e:
            raise Exception(f"GitHub PAT validation failed: {e}")

    async def test(self, token_data: dict) -> bool:
        pat = token_data.get("access_token", "")
        if not pat:
            return False
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    GITHUB_USER_URL,
                    headers={
                        "Authorization": f"token {pat}",
                        "Accept": "application/vnd.github.v3+json",
                    },
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    return resp.status == 200
        except Exception:
            return False

    async def get_user_info(self, token_data: dict) -> dict:
        return token_data.get("user", {})

    def parse_error(self, status_code: int, body: dict) -> str:
        if status_code == 401:
            return "Invalid GitHub Personal Access Token. Please check and re-paste."
        return f"GitHub error (HTTP {status_code}): {body.get('message', '')}"
