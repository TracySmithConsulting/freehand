from typing import Dict, List, Optional


class BaseConnector:
    service: str = ""
    name: str = ""
    icon: str = ""
    scopes_read: List[str] = []
    scopes_write: List[str] = []
    supports_multi_account: bool = True

    def authorize_url(self, state: str, redirect_uri: str) -> str:
        raise NotImplementedError

    def handle_callback(self, code: str, state: str, redirect_uri: str) -> dict:
        raise NotImplementedError

    def refresh(self, token_data: dict) -> dict:
        raise NotImplementedError

    def test(self, token_data: dict) -> bool:
        raise NotImplementedError

    def get_user_info(self, token_data: dict) -> dict:
        raise NotImplementedError

    def parse_error(self, status_code: int, body: dict) -> str:
        return f"Authentication error (HTTP {status_code})"
