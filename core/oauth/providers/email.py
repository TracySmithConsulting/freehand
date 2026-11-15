import ssl
import socket
from typing import Dict
import imaplib
import smtplib

from core.oauth.connector import BaseConnector


class EmailConnector(BaseConnector):
    service = "email"
    name = "Email (IMAP/SMTP)"
    icon = "@"
    scopes_read = ["imap", "smtp"]
    scopes_write = ["smtp"]
    supports_multi_account = True
    uses_password = True

    def authorize_url(self, state: str, redirect_uri: str) -> str:
        return ""

    async def handle_callback(self, code: str, state: str, redirect_uri: str) -> dict:
        return {}

    def test_credentials(self, credentials: dict) -> dict:
        from core.tools import own_email
        r = own_email.test(credentials)
        return {"imap_ok": r["reading"], "smtp_ok": r["sending"], "errors": r["problems"]}

    async def test(self, token_data: dict) -> bool:
        creds = token_data.get("credentials", {})
        if not creds:
            return False
        results = self.test_credentials(creds)
        return results.get("imap_ok", False) and results.get("smtp_ok", False)

    async def get_user_info(self, token_data: dict) -> dict:
        creds = token_data.get("credentials", {})
        return {"email": creds.get("username", "")}

    def parse_error(self, status_code: int, body: dict) -> str:
        return "Email authentication error"
