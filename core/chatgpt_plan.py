"""Use your ChatGPT plan: Sign in with ChatGPT (Nov 2026).

Since September 2026 OpenAI lets open-source apps that run on the
person's own computer use their ChatGPT Plus or Pro plan, instead of a
key they pay for separately
(https://developers.openai.com/siwc/token-sharing-open-source). FreeHand
is one, so people who already pay for ChatGPT don't pay again.

How it works:
- **Each person signs in with their own ChatGPT.** A ChatGPT sign-in
  belongs to one person, so it's only used for that person's chats and
  jobs, never for anyone else on this FreeHand. Their ChatGPT comes first;
  FreeHand's usual models (Settings, AI model) are the backups.
- **Signing in** is OAuth with PKCE in the browser. The first time,
  FreeHand registers itself with OpenAI as "FreeHand" for that person
  (``client_id=dynamic_agent_client``) and keeps the client ID OpenAI
  issues. OpenAI returns to ``http://127.0.0.1:<port>/auth/callback``:
  FreeHand's own address on this computer. On another device that
  address doesn't reach FreeHand, so the page lets people paste the
  address the browser ended up on instead (``finish_from_address``).
- **The ID token is checked** against OpenAI's published keys (issuer,
  audience, expiry, nonce) before anything is kept, and the plan is only
  used if the person allowed it (the ``chatgpt.tokens.use.direct`` scope).
- **Tokens are kept encrypted** in the vault (core/oauth/manager.py's
  key), never shown on the page, never logged. Signing out revokes the
  session at OpenAI.
- **Asking ChatGPT** uses OpenAI's Responses API, which works differently
  from the chat API FreeHand uses everywhere else. ``call`` turns
  FreeHand's messages and tools into that form and the answer back, so
  the rest of FreeHand doesn't know the difference. The preview's rules
  (store false, stream true, no temperature or length limit, tools in a
  namespace) are followed exactly.
- **Limits** are the person's own: ChatGPT counts this use against their
  plan, and they can set a weekly limit for FreeHand at
  chatgpt.com/settings/usage. When it's used up, FreeHand says so and
  uses its usual models; nothing is ever charged to anyone.

OpenAI's open-source rules don't cover FreeHands hosted for clients, so
hosted tenants turn this off (FREEHAND_CHATGPT_PLAN=0, set by
scripts/provision_tenant.py).
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import secrets
import threading
import time
import urllib.parse
import uuid
from typing import Optional

log = logging.getLogger(__name__)

AUTH = "https://auth.openai.com"
AUTHORIZE_URL = AUTH + "/api/accounts/authorize"
TOKEN_URL = AUTH + "/api/accounts/oauth/token"
DISCOVERY_URL = AUTH + "/.well-known/openid-configuration"
API = "https://api.openai.com/v1"
RESOURCE = "https://api.openai.com/v1"
PLAN_SCOPE = "chatgpt.tokens.use.direct"
SCOPES = "openid profile email offline_access resource.invoke " + PLAN_SCOPE
APP_NAME = "FreeHand"
USAGE_URL = "https://chatgpt.com/settings/usage"
CALLBACK_PATH = "/auth/callback"
DEFAULT_PORT = 1455  # OpenAI's example; any port works, only the path is fixed
NAMESPACE = "freehand"
PENDING_SECONDS = 600
REFRESH_EARLY_SECONDS = 300
FILE_NAME = "chatgpt_accounts.json"

_lock = threading.Lock()
_refresh_locks: dict = {}
_pending: dict = {}  # state -> the sign-in being started
_keys_cache: dict = {"at": 0.0, "discovery": None, "jwks": None}


class SignInError(Exception):
    """A sign-in that didn't finish, in words to show the person."""


def available() -> bool:
    """Off on hosted FreeHands (see the module notes)."""
    return os.environ.get("FREEHAND_CHATGPT_PLAN", "1").strip().lower() not in ("0", "false", "no", "off")


# ── Keeping the sign-ins ─────────────────────────────────────────────

def _path():
    from core import agent_config
    return agent_config.VAULT_DIR / FILE_NAME


def _read() -> dict:
    """{"host_id": ..., "accounts": {person: record}}."""
    from core.oauth import manager
    path = _path()
    if not path.exists():
        return {"host_id": "", "accounts": {}}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        accounts = json.loads(manager.decrypt(raw["accounts"])) if raw.get("accounts") else {}
    except Exception:
        log.warning("The ChatGPT sign-ins couldn't be read; they'll need signing in again.")
        return {"host_id": "", "accounts": {}}
    return {"host_id": str(raw.get("host_id") or ""), "accounts": accounts if isinstance(accounts, dict) else {}}


def _write(data: dict) -> None:
    from core.oauth import manager
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps({"host_id": data.get("host_id", ""),
                       "accounts": manager.encrypt(json.dumps(data.get("accounts") or {}))})
    tmp = path.with_suffix(".tmp")
    tmp.write_text(body, encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)


def host_id() -> str:
    """This FreeHand's own ID for OpenAI: chosen once, before the first
    sign-in, and kept. Random, so it says nothing about anyone."""
    with _lock:
        data = _read()
        if not data["host_id"]:
            data["host_id"] = f"urn:uuid:{uuid.uuid4()}"
            _write(data)
        return data["host_id"]


def _account(person: str) -> Optional[dict]:
    acc = _read()["accounts"].get(person)
    return acc if isinstance(acc, dict) else None


def _save_account(person: str, record: dict) -> None:
    with _lock:
        data = _read()
        data["accounts"][person] = record
        _write(data)


def signed_in(person: str) -> bool:
    """Can this person's ChatGPT plan be used right now?"""
    if not available():
        return False
    acc = _account(person)
    return bool(acc and acc.get("refresh_token") and PLAN_SCOPE in (acc.get("scopes") or []))


def anyone_signed_in() -> bool:
    if not available():
        return False
    return any(signed_in(p) for p in _read()["accounts"])


def status(person: str) -> dict:
    """For the page. Never includes a token."""
    acc = _account(person) or {}
    out = {
        "available": available(),
        "signed_in": signed_in(person),
        "email": acc.get("email", "") if acc.get("refresh_token") else "",
        "model": acc.get("model", ""),
        "models": acc.get("models") or [],
        "plan_allowed": PLAN_SCOPE in (acc.get("scopes") or []),
        "welcomed": bool(acc.get("welcomed")),
        "usage_url": USAGE_URL,
        "problem": acc.get("problem", ""),
    }
    if acc.get("refresh_token") and not out["plan_allowed"]:
        out["problem"] = ("You signed in, but didn't allow FreeHand to use your ChatGPT plan. "
                          "Choose Continue with ChatGPT again and allow it.")
    return out


def set_model(person: str, model: str) -> dict:
    acc = _account(person)
    if not acc or not acc.get("refresh_token"):
        raise SignInError("Sign in with ChatGPT first.")
    slugs = [m.get("slug") for m in acc.get("models") or []]
    if slugs and model not in slugs:
        raise SignInError("ChatGPT doesn't offer that model to your account. Choose one from the list.")
    acc["model"] = model
    _save_account(person, acc)
    return status(person)


def welcomed(person: str) -> None:
    acc = _account(person)
    if acc:
        acc["welcomed"] = True
        _save_account(person, acc)


# ── Signing in ───────────────────────────────────────────────────────

def redirect_uri(host: str, port: Optional[int]) -> str:
    """OpenAI only returns to 127.0.0.1. When FreeHand was opened on this
    computer, that's FreeHand itself; otherwise the person pastes the
    address back (finish_from_address), so the port doesn't matter."""
    local = (host or "").strip("[]").lower() in ("127.0.0.1", "localhost", "::1")
    return f"http://127.0.0.1:{port if local and port else DEFAULT_PORT}{CALLBACK_PATH}"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def start(person: str, callback: str) -> str:
    """The address to open in the browser for this person's sign-in."""
    if not available():
        raise SignInError("Using a ChatGPT plan isn't available on this FreeHand.")
    now = time.time()
    for s, p in list(_pending.items()):
        if now - p["at"] > PENDING_SECONDS:
            _pending.pop(s, None)
    acc = _account(person) or {}
    verifier = secrets.token_urlsafe(48)
    state, nonce = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    issued = acc.get("client_id", "")
    params = {
        "client_id": issued or "dynamic_agent_client",
        "ext_agent_host_id": host_id(),
        "response_type": "code",
        "redirect_uri": callback,
        "scope": SCOPES,
        "resource": RESOURCE,
        "state": state,
        "nonce": nonce,
        "code_challenge_method": "S256",
        "code_challenge": _b64url(hashlib.sha256(verifier.encode()).digest()),
    }
    if issued:
        # Signing in again: the same registration, and its account.
        if acc.get("id_token"):
            params["id_token_hint"] = acc["id_token"]
        if acc.get("email"):
            params["login_hint"] = acc["email"]
        if acc.get("refresh_token") and PLAN_SCOPE not in (acc.get("scopes") or []):
            params["prompt"] = "consent"  # they declined the plan before: ask again
    else:
        params["agent_name_hint"] = APP_NAME
    _pending[state] = {"person": person, "verifier": verifier, "nonce": nonce, "callback": callback,
                       "client_id": issued, "at": now}
    return AUTHORIZE_URL + "?" + urllib.parse.urlencode(params, quote_via=urllib.parse.quote)


def pending_person(state: str) -> Optional[str]:
    p = _pending.get(state or "")
    return p["person"] if p else None


async def finish(code: str, state: str, client_id: str = "", error: str = "", error_description: str = "") -> dict:
    """OpenAI came back: check everything, then keep the sign-in.
    Returns status() for the person; raises SignInError in plain words."""
    pending = _pending.pop(state, None) if state else None
    if not pending or time.time() - pending["at"] > PENDING_SECONDS:
        raise SignInError("This sign-in is old or wasn't started from FreeHand. Start again from FreeHand.")
    if error:
        log.info("ChatGPT sign-in came back with %s: %s", error, error_description[:300])
        if error == "access_denied":
            raise SignInError("You didn't allow FreeHand, so nothing changed. "
                              "You can choose Continue with ChatGPT again whenever you like.")
        raise SignInError("ChatGPT didn't sign you in. Try again in a minute.")
    if not code:
        raise SignInError("ChatGPT didn't send back a sign-in code. Try again.")
    person = pending["person"]
    issued = pending["client_id"]
    if issued and client_id and client_id != issued:
        raise SignInError("That sign-in was for a different registration. Start again from FreeHand.")
    issued = issued or client_id
    if not issued or issued == "dynamic_agent_client":
        raise SignInError("ChatGPT didn't finish registering FreeHand. Try again.")

    status_code, tokens = await _post_form_status(TOKEN_URL, {
        "grant_type": "authorization_code", "client_id": issued, "code": code,
        "code_verifier": pending["verifier"], "redirect_uri": pending["callback"], "resource": RESOURCE,
    })
    if status_code != 200 or not isinstance(tokens, dict) or not tokens.get("access_token"):
        why = {k: tokens.get(k) for k in ("error", "error_description", "detail")} if isinstance(tokens, dict) else tokens
        log.warning("ChatGPT refused the sign-in code (%s): %s", status_code, str(why)[:300])
        raise SignInError("FreeHand couldn't finish signing in with ChatGPT. Try again.")
    claims = await verify_id_token(tokens.get("id_token", ""), issued, pending["nonce"])
    old = _account(person) or {}
    if old.get("subject") and old.get("client_id") == issued and old["subject"] != claims.get("sub"):
        raise SignInError("That's a different ChatGPT account from the one you signed in with before. "
                          "Sign out first if you want to change accounts.")
    scopes = sorted(str(tokens.get("scope") or "").split())
    record = {
        **{k: old.get(k) for k in ("model", "welcomed") if k in old},
        "email": str(claims.get("email") or ""),
        "subject": str(claims.get("sub") or ""),
        "issuer": AUTH,
        "client_id": issued,
        "id_token": tokens.get("id_token", ""),
        "access_token": tokens["access_token"],
        "refresh_token": tokens.get("refresh_token", ""),
        "expires_at": time.time() + float(tokens.get("expires_in") or 3600),
        "scopes": scopes,
        "saved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "problem": "",
    }
    if old.get("subject") != record["subject"]:
        record.pop("welcomed", None)
    _save_account(person, record)
    if PLAN_SCOPE in scopes:
        await refresh_models(person)
    return status(person)


async def finish_from_address(person: str, address: str) -> dict:
    """The person pasted the address the browser ended up on (FreeHand
    opened on another device, so 127.0.0.1 didn't reach it)."""
    parts = urllib.parse.urlsplit((address or "").strip())
    if parts.path.rstrip("/") != CALLBACK_PATH:
        raise SignInError("That isn't the address ChatGPT sent you to. Copy the whole address from the "
                          "browser's address bar after you approve FreeHand.")
    q = urllib.parse.parse_qs(parts.query)
    state = (q.get("state") or [""])[0]
    if pending_person(state) != person:
        raise SignInError("This sign-in is old or wasn't started from FreeHand. Start again from FreeHand.")
    return await finish((q.get("code") or [""])[0], state, (q.get("client_id") or [""])[0],
                        (q.get("error") or [""])[0], (q.get("error_description") or [""])[0])


async def sign_out(person: str) -> dict:
    """Stop using their plan: end the session at OpenAI, forget the
    tokens. The registration stays, so signing in again is quick."""
    acc = _account(person)
    if not acc or not acc.get("refresh_token"):
        return {"revoked": True}
    revoked = await _revoke(acc)
    for k in ("access_token", "refresh_token", "id_token", "expires_at", "scopes", "models", "email", "problem"):
        acc.pop(k, None)
    _save_account(person, acc)
    return {"revoked": revoked}


async def forget(person: str) -> bool:
    """They've left this FreeHand: sign out and remove the registration."""
    acc = _account(person)
    revoked = await _revoke(acc) if acc and acc.get("refresh_token") else True
    with _lock:
        data = _read()
        data["accounts"].pop(person, None)
        _write(data)
    return revoked


async def _revoke(acc: dict) -> bool:
    discovery = await _discovery()
    endpoint = (discovery or {}).get("revocation_endpoint")
    if not endpoint:
        return False
    for delay in (0, 1, 3):
        if delay:
            await asyncio.sleep(delay)
        status_code, _ = await _post_form_status(endpoint, {
            "token": acc.get("refresh_token", ""), "token_type_hint": "refresh_token",
            "client_id": acc.get("client_id", ""),
        })
        if status_code == 200:
            return True
        if status_code and status_code < 500:
            return False
    return False


# ── Checking the ID token (RS256, OpenAI's published keys) ───────────

async def _discovery() -> Optional[dict]:
    if _keys_cache["discovery"] and time.time() - _keys_cache["at"] < 3600:
        return _keys_cache["discovery"]
    found = await _get_json(DISCOVERY_URL)
    if found:
        _keys_cache.update(discovery=found, at=time.time(), jwks=None)
    return found


async def _jwks(refresh: bool = False) -> list:
    if _keys_cache["jwks"] and not refresh:
        return _keys_cache["jwks"]
    discovery = await _discovery() or {}
    keys = (await _get_json(discovery.get("jwks_uri") or AUTH + "/.well-known/jwks.json") or {}).get("keys") or []
    _keys_cache["jwks"] = keys
    return keys


def _unb64(part: str) -> bytes:
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


def check_jwt(token: str, keys: list, audience: str, nonce: str, now: Optional[float] = None) -> dict:
    """The token's claims, if it's signed by one of ``keys`` and is for
    ``audience``, from OpenAI, unexpired and carries ``nonce``."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding, rsa
    try:
        head_b64, body_b64, sig_b64 = token.split(".")
        header = json.loads(_unb64(head_b64))
        claims = json.loads(_unb64(body_b64))
        signature = _unb64(sig_b64)
    except (ValueError, TypeError):
        raise SignInError("ChatGPT's sign-in reply couldn't be read. Try again.")
    if header.get("alg") != "RS256":
        raise SignInError("ChatGPT's sign-in reply wasn't signed the expected way. Try again.")
    key = next((k for k in keys if k.get("kid") == header.get("kid") and k.get("kty") == "RSA"), None)
    if not key:
        raise LookupError("unknown key")
    public = rsa.RSAPublicNumbers(int.from_bytes(_unb64(key["e"]), "big"),
                                  int.from_bytes(_unb64(key["n"]), "big")).public_key()
    try:
        public.verify(signature, f"{head_b64}.{body_b64}".encode(), padding.PKCS1v15(), hashes.SHA256())
    except InvalidSignature:
        raise SignInError("ChatGPT's sign-in reply didn't check out, so FreeHand didn't keep it. Try again.")
    now = time.time() if now is None else now
    aud = claims.get("aud")
    if claims.get("iss") != AUTH or audience not in (aud if isinstance(aud, list) else [aud]):
        raise SignInError("That sign-in reply wasn't for FreeHand, so FreeHand didn't keep it. Try again.")
    if float(claims.get("exp") or 0) < now - 60:
        raise SignInError("That sign-in reply had expired. Try again.")
    if not nonce or not secrets.compare_digest(str(claims.get("nonce") or ""), nonce):
        raise SignInError("That sign-in reply didn't match the sign-in FreeHand started. Try again.")
    return claims


async def verify_id_token(token: str, audience: str, nonce: str) -> dict:
    if not token:
        raise SignInError("ChatGPT didn't say who signed in. Try again.")
    try:
        return check_jwt(token, await _jwks(), audience, nonce)
    except LookupError:
        try:  # OpenAI may have rotated its keys
            return check_jwt(token, await _jwks(refresh=True), audience, nonce)
        except LookupError:
            raise SignInError("FreeHand couldn't check ChatGPT's sign-in reply. Try again in a minute.")


# ── Tokens ───────────────────────────────────────────────────────────

async def access_token(person: str) -> Optional[str]:
    """A current access token, renewed when it's nearly out of date.
    None when they need to sign in again (the reason is kept for the page)."""
    acc = _account(person)
    if not acc or not acc.get("refresh_token"):
        return None
    if float(acc.get("expires_at") or 0) - time.time() > REFRESH_EARLY_SECONDS:
        return acc["access_token"]
    # Refresh tokens rotate: never two refreshes at once. Jobs run in their
    # own event loops (core/scheduler.py), and a lock belongs to one loop.
    lock = _refresh_locks.setdefault((id(asyncio.get_running_loop()), person), asyncio.Lock())
    async with lock:
        acc = _account(person) or {}
        if float(acc.get("expires_at") or 0) - time.time() > REFRESH_EARLY_SECONDS:
            return acc.get("access_token")
        status_code, data = await _post_form_status(TOKEN_URL, {
            "grant_type": "refresh_token", "client_id": acc.get("client_id", ""),
            "refresh_token": acc.get("refresh_token", ""), "resource": RESOURCE,
        })
        if status_code == 200 and isinstance(data, dict) and data.get("access_token"):
            acc.update(access_token=data["access_token"],
                       refresh_token=data.get("refresh_token") or acc.get("refresh_token"),
                       expires_at=time.time() + float(data.get("expires_in") or 3600),
                       scopes=sorted(str(data.get("scope") or " ".join(acc.get("scopes") or [])).split()),
                       problem="")
            _save_account(person, acc)
            return acc["access_token"]
        code = str((data or {}).get("error") or "") if isinstance(data, dict) else ""
        if status_code and 400 <= status_code < 500:
            # Unusable (expired, revoked, disconnected in ChatGPT): sign in again.
            for k in ("access_token", "refresh_token", "expires_at"):
                acc.pop(k, None)
            acc["problem"] = ("Your ChatGPT sign-in has ended (for example, FreeHand was disconnected in "
                              "ChatGPT's settings). Choose Continue with ChatGPT to sign in again.")
            _save_account(person, acc)
            log.info("ChatGPT refresh refused for a person (%s)", code or status_code)
        return None


async def refresh_models(person: str) -> list:
    """The models ChatGPT offers this account, in its own order."""
    token = await access_token(person)
    if not token:
        return []
    data = await _get_json(f"{API}/models", headers={"Authorization": f"Bearer {token}"})
    items = (data or {}).get("models") or (data or {}).get("data") or []
    models = [{"slug": str(m.get("slug") or m.get("id")), "name": str(m.get("display_name") or m.get("slug") or m.get("id"))}
              for m in items if isinstance(m, dict) and (m.get("slug") or m.get("id"))
              and m.get("visibility", "list") == "list"]
    acc = _account(person)
    if acc and models:
        acc["models"] = models
        if acc.get("model") not in [m["slug"] for m in models]:
            acc["model"] = models[0]["slug"]
        _save_account(person, acc)
    return models


# ── Asking ChatGPT (the Responses API) ───────────────────────────────

def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(p.get("text") or "") for p in content if isinstance(p, dict) and p.get("text"))
    return "" if content is None else str(content)


def _user_content(content):
    if not isinstance(content, list):
        return _text(content)
    parts = []
    for p in content:
        if not isinstance(p, dict):
            continue
        if p.get("type") == "text":
            parts.append({"type": "input_text", "text": str(p.get("text") or "")})
        elif p.get("type") == "image_url":
            url = p.get("image_url", {}).get("url") if isinstance(p.get("image_url"), dict) else p.get("image_url")
            if url:
                parts.append({"type": "input_image", "image_url": url})
    return parts or ""


def to_request(messages: list, tools: Optional[list], model: str) -> dict:
    """FreeHand's chat messages and tools, as a Responses request."""
    instructions, items = [], []
    for m in messages or []:
        role, content = m.get("role"), m.get("content")
        if role in ("system", "developer"):
            # The preview rejects system messages: they go in instructions.
            if _text(content):
                instructions.append(_text(content))
        elif role == "tool":
            items.append({"type": "function_call_output", "call_id": str(m.get("tool_call_id") or ""),
                          "output": _text(content)})
        elif role == "assistant":
            if _text(content):
                items.append({"role": "assistant", "content": _text(content)})
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function") or {}
                items.append({"type": "function_call", "call_id": str(tc.get("id") or ""), "namespace": NAMESPACE,
                              "name": str(fn.get("name") or ""), "arguments": fn.get("arguments") or "{}"})
        else:
            items.append({"role": "user", "content": _user_content(content)})
    body = {"model": model, "input": items, "store": False, "stream": True}
    if instructions:
        body["instructions"] = "\n\n".join(instructions)
    functions = []
    for t in tools or []:
        fn = t.get("function") if isinstance(t, dict) else None
        if fn and fn.get("name"):
            functions.append({"type": "function", "name": fn["name"], "description": fn.get("description", ""),
                              "parameters": fn.get("parameters") or {"type": "object", "properties": {}},
                              "strict": False})  # as with every other model: optional stays optional
    if functions:
        # The preview wants function tools grouped in a namespace.
        body["tools"] = [{"type": "namespace", "name": NAMESPACE,
                          "description": "FreeHand's tools for helping this person.", "tools": functions}]
        body["tool_choice"] = "auto"
    return body


def from_response(response: dict) -> dict:
    """A completed Responses answer, as a chat message."""
    text, calls = [], []
    for item in response.get("output") or []:
        if item.get("type") == "message":
            for part in item.get("content") or []:
                if part.get("type") == "output_text":
                    text.append(part.get("text") or "")
        elif item.get("type") == "function_call":
            calls.append({"id": item.get("call_id") or item.get("id") or "", "type": "function",
                          "function": {"name": item.get("name", ""), "arguments": item.get("arguments") or "{}"}})
    msg = {"role": "assistant", "content": "".join(text) or None}
    if calls:
        msg["tool_calls"] = calls
    usage = response.get("usage") or {}
    msg["_usage"] = {"prompt_tokens": usage.get("input_tokens", 0), "completion_tokens": usage.get("output_tokens", 0)}
    return msg


def explain(status_code: Optional[int], code: str = "") -> str:
    """ChatGPT's refusal, in plain words."""
    if code == "subscription_sharing_usage_limit_exceeded" or status_code == 429:
        return ("Your ChatGPT plan's limit for FreeHand is used up for now. You can check it or change "
                f"FreeHand's limit at {USAGE_URL}")
    if code == "subscription_sharing_user_not_eligible":
        return "Your ChatGPT account or plan can't be used in other apps."
    if code in ("subscription_sharing_usage_unavailable", "subscription_sharing_user_unavailable") or status_code == 503:
        return "ChatGPT couldn't check your plan just now. Try again in a minute."
    if code == "subscription_sharing_unsupported_capability":
        return "ChatGPT can't do that with your plan (a model or feature it doesn't allow here)."
    if status_code == 401 or code == "subscription_sharing_invalid_user":
        return "ChatGPT didn't accept your sign-in. Choose Continue with ChatGPT to sign in again."
    if status_code == 403:
        return "ChatGPT refused (for example, it isn't offered in your country)."
    if status_code is None:
        return "FreeHand couldn't reach ChatGPT. Check the internet connection."
    if status_code >= 500:
        return "ChatGPT had a problem on its side. Try again in a minute."
    return f"ChatGPT refused the request (error {status_code})."


async def call(messages: list, tools: Optional[list], person: str, timeout_seconds: float = 120) -> tuple:
    """Ask ChatGPT on this person's plan. Returns (message or {"error"},
    HTTP status or None, timed out?), like agent._call_one."""
    acc = _account(person) or {}
    token = await access_token(person)
    if not token:
        return {"error": "Your ChatGPT sign-in has ended. Choose Continue with ChatGPT to sign in again."}, 401, False
    model = acc.get("model") or ""
    if not model:
        models = await refresh_models(person)
        model = models[0]["slug"] if models else ""
    if not model:
        return {"error": "ChatGPT didn't offer any models to your account."}, 404, False
    return await _send(to_request(messages, tools, model), token, timeout_seconds)


async def _send(body: dict, token: str, timeout_seconds: float) -> tuple:
    """POST /v1/responses and read the stream (tests stand in for this)."""
    import aiohttp
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json", "Accept": "text/event-stream"}
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout_seconds)) as session:
            async with session.post(f"{API}/responses", json=body, headers=headers) as resp:
                if resp.status != 200:
                    try:
                        data = await resp.json(content_type=None)
                    except Exception:
                        data = {}
                    err = data.get("error") if isinstance(data, dict) else None
                    code = str(err.get("code") or "") if isinstance(err, dict) else ""
                    log.info("ChatGPT refused (%s %s, request %s)", resp.status, code,
                             resp.headers.get("x-request-id", "?"))
                    return {"error": explain(resp.status, code)}, resp.status, False
                return await _read_stream(resp)
    except asyncio.TimeoutError:
        return {"error": f"ChatGPT didn't answer in time ({int(timeout_seconds)}s)."}, None, True
    except aiohttp.ClientError:
        return {"error": explain(None)}, None, False


async def _read_stream(resp) -> tuple:
    """Read the events through to the end: only response.completed counts."""
    buffer = ""
    async for chunk in resp.content.iter_any():
        buffer += chunk.decode("utf-8", "replace")
        while "\n\n" in buffer:
            event, buffer = buffer.split("\n\n", 1)
            result = _event(event)
            if result is not None:
                return result
    if buffer.strip():
        result = _event(buffer)
        if result is not None:
            return result
    return {"error": "ChatGPT stopped before it finished answering. Try again."}, None, False


def _event(raw: str) -> Optional[tuple]:
    data = "\n".join(line[5:].strip() for line in raw.splitlines() if line.startswith("data:"))
    if not data or data == "[DONE]":
        return None
    try:
        ev = json.loads(data)
    except ValueError:
        return None
    kind = ev.get("type", "")
    if kind == "response.completed":
        return from_response(ev.get("response") or {}), 200, False
    if kind == "response.failed":
        err = (ev.get("response") or {}).get("error") or {}
        code = str(err.get("code") or "")
        status_code = 429 if code == "subscription_sharing_usage_limit_exceeded" else 503 \
            if code.endswith("_unavailable") else 400
        return {"error": explain(status_code, code)}, status_code, False
    if kind == "response.incomplete":
        return {"error": "ChatGPT stopped part-way through its answer. Try again."}, 400, False
    if kind == "error":
        code = str(ev.get("code") or (ev.get("error") or {}).get("code") or "")
        return {"error": explain(400, code)}, 400, False
    return None


# ── HTTP helpers ─────────────────────────────────────────────────────

async def _get_json(url: str, headers: Optional[dict] = None) -> Optional[dict]:
    import aiohttp
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
            async with session.get(url, headers={"Accept": "application/json", **(headers or {})}) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json(content_type=None)
                return data if isinstance(data, dict) else None
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        return None


async def _post_form_status(url: str, form: dict) -> tuple:
    import aiohttp
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
            async with session.post(url, data=form, headers={"Accept": "application/json"}) as resp:
                try:
                    data = await resp.json(content_type=None)
                except Exception:
                    data = None
                return resp.status, data
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return None, None


async def _post_form(url: str, form: dict) -> Optional[dict]:
    status_code, data = await _post_form_status(url, form)
    return data if status_code == 200 and isinstance(data, dict) else None
