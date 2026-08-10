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
        host = credentials.get("host", "")
        port = int(credentials.get("port", 993))
        username = credentials.get("username", "")
        password = credentials.get("password", "")
        use_ssl = credentials.get("use_ssl", True)

        results = {"imap_ok": False, "smtp_ok": False, "errors": []}

        if not host or not username or not password:
            return {"imap_ok": False, "smtp_ok": False, "errors": ["Missing host, username, or password"]}

        try:
            if use_ssl:
                imap = imaplib.IMAP4_SSL(host, port)
            else:
                imap = imaplib.IMAP4(host, port)
            imap.login(username, password)
            imap.select("INBOX")
            imap.logout()
            results["imap_ok"] = True
        except Exception as e:
            results["errors"].append(f"IMAP: {e}")

        try:
            if use_ssl:
                smtp = smtplib.SMTP_SSL(host, port)
            else:
                smtp = smtplib.SMTP(host, port)
            smtp.login(username, password)
            smtp.quit()
            results["smtp_ok"] = True
        except Exception as e:
            results["errors"].append(f"SMTP: {e}")

        return results

    async def test(self, token_data: dict) -> bool:
        creds = token_data.get("credentials", {})
        if not creds:
            return False
        results = self.test_credentials(creds)
        return results.get("imap_ok", False) or results.get("smtp_ok", False)

    async def get_user_info(self, token_data: dict) -> dict:
        creds = token_data.get("credentials", {})
        return {"email": creds.get("username", "")}

    def parse_error(self, status_code: int, body: dict) -> str:
        return "Email authentication error"
