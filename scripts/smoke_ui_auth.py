#!/usr/bin/env python
"""Live smoke: browser auth plumbing against the running FreeHand server.

No shell quoting (MSYS mangles $()). Hits 127.0.0.1:8000 exactly as the
browser would:
  1. WITHOUT the key  -> integrations open, approvals/tier 401
  2. WITH the key     -> all three 200
  3. served HTML      -> carries the new plumbing
The key is read from vault/settings.json and NEVER printed.
"""
import json
import sys
import urllib.request
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASE = "http://127.0.0.1:8000"
PYEXE_HINT = "vault/settings.json"


def http(path, key=None, method="GET", body=None):
    """Return the status code for a request to BASE+path."""
    req = urllib.request.Request(
        BASE + path,
        method=method,
        data=(json.dumps(body).encode() if body is not None else None),
    )
    req.add_header("Content-Type", "application/json")
    if key:
        req.add_header("X-API-Key", key)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            r.read()
            return r.status
    except urllib.error.HTTPError as e:
        e.read()
        return e.code


def main():
    key = json.loads((ROOT / PYEXE_HINT).read_text())["api_key"]

    print("=== browser WITHOUT key (fresh visitor — no X-API-Key header) ===")
    for ep in ["/api/integrations", "/api/approvals", "/api/security/tier"]:
        code = http(ep)
        print(f"  {ep:<20} -> {code}")

    print("=== browser WITH key (what apiFetch does after 'Remember key') ===")
    for ep in ["/api/integrations", "/api/approvals", "/api/security/tier"]:
        code = http(ep, key=key)
        print(f"  {ep:<20} -> {code}")

    print("=== served console HTML carries the new plumbing? ===")
    html = urllib.request.urlopen(BASE + "/", timeout=15).read().decode()
    markers = [m for m in ("freehand_api_key", "remember-key-btn", "forget-key-btn", "startApprovalsPolling") if m in html]
    for m in markers:
        print(f"  found: {m}")
    missing = ("freehand_api_key", "remember-key-btn", "forget-key-btn", "startApprovalsPolling")
    absent = [m for m in missing if m not in markers]
    print("SMOKE:", "PASS" if (not absent and "startApprovalsPolling" in markers) else f"FAIL (missing {absent})")


if __name__ == "__main__":
    main()
