"""AI model services FreeHand knows how to set up (Nov 2026).

Setting up a model used to mean knowing a provider's API address, its key
format and the exact model name. Now people pick a service (or just paste
a key and FreeHand works out which service it is for), choose a model from
a list, and press "Test it".

Every service here speaks the OpenAI-compatible chat API, which is what
FreeHand uses (core/agent.py call_llm).
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Optional

PROVIDERS: list[dict] = [
    {
        "id": "openrouter",
        "name": "OpenRouter",
        "base_url": "https://openrouter.ai/api/v1",
        "key_prefixes": ["sk-or-"],
        "key_url": "https://openrouter.ai/settings/keys",
        "signin": True,
        "default_model": "openrouter/auto",
        "blurb": "One account for models from many companies (Claude, GPT, Gemini, MiniMax and more). Easiest: sign in, no key to copy.",
        "steps": ["Choose Sign in with OpenRouter.", "Create an account or sign in, and add some credit.", "Approve FreeHand. That's it."],
    },
    {
        "id": "openai",
        "name": "OpenAI (ChatGPT models)",
        "base_url": "https://api.openai.com/v1",
        "key_prefixes": ["sk-proj-", "sk-svcacct-", "sk-admin-"],
        "key_url": "https://platform.openai.com/api-keys",
        "default_model": "gpt-4o-mini",
        "blurb": "GPT models. A ChatGPT subscription doesn't include this; it is billed separately.",
        "steps": ["Open the key page and sign in.", "Add a payment method under Billing.", "Choose Create new secret key, copy it, and paste it here."],
    },
    {
        "id": "anthropic",
        "name": "Anthropic (Claude)",
        "base_url": "https://api.anthropic.com/v1",
        "key_prefixes": ["sk-ant-"],
        "key_url": "https://platform.claude.com/settings/keys",
        "default_model": "claude-sonnet-4-6",
        # Newer Claude models reject temperature values below 1 through this
        # compatible API (Anthropic's OpenAI SDK compatibility docs).
        "omit_temperature": True,
        "blurb": "Claude models. A Claude.ai subscription doesn't include this; it is billed separately. "
                 "Anthropic describes this connection as mainly for testing; OpenRouter is a steadier way to use Claude.",
        "steps": ["Open the key page and sign in.", "Add credit under Billing.", "Choose Create Key, copy it, and paste it here."],
    },
    {
        "id": "google",
        "name": "Google Gemini",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "key_prefixes": ["AIza"],
        "key_url": "https://aistudio.google.com/apikey",
        "default_model": "gemini-2.5-flash",
        "blurb": "Gemini models. Google offers a free allowance to start.",
        "steps": ["Open the key page and sign in with Google.", "Choose Create API key.", "Copy it and paste it here."],
    },
    {
        "id": "minimax",
        "name": "MiniMax",
        "base_url": "https://api.minimax.io/v1",
        "key_prefixes": [],
        "key_url": "https://platform.minimax.io",
        "default_model": "MiniMax-M2.5",
        "blurb": "MiniMax models, good value.",
        "steps": ["Open the MiniMax platform and sign in.", "Find API Keys in your account and create a key.", "Copy it and paste it here."],
    },
    {
        "id": "groq",
        "name": "Groq",
        "base_url": "https://api.groq.com/openai/v1",
        "key_prefixes": ["gsk_"],
        "key_url": "https://console.groq.com/keys",
        "default_model": "llama-3.3-70b-versatile",
        "blurb": "Very fast open models, with a free allowance.",
        "steps": ["Open the key page and sign in.", "Choose Create API Key.", "Copy it and paste it here."],
    },
    {
        "id": "deepseek",
        "name": "DeepSeek",
        "base_url": "https://api.deepseek.com/v1",
        "key_prefixes": [],
        "key_url": "https://platform.deepseek.com/api_keys",
        "default_model": "deepseek-chat",
        "blurb": "DeepSeek models, low cost.",
        "steps": ["Open the key page and sign in.", "Add credit, then create an API key.", "Copy it and paste it here."],
    },
    {
        "id": "mistral",
        "name": "Mistral",
        "base_url": "https://api.mistral.ai/v1",
        "key_prefixes": [],
        "key_url": "https://console.mistral.ai/api-keys",
        "default_model": "mistral-small-latest",
        "blurb": "Mistral models, from Europe.",
        "steps": ["Open the key page and sign in.", "Create a new key.", "Copy it and paste it here."],
    },
    {
        "id": "ollama",
        "name": "Ollama (free, on this computer)",
        "base_url": "http://host.docker.internal:11434/v1",
        "key_prefixes": [],
        "key_url": "https://ollama.com/download",
        "no_key": True,
        "default_model": "llama3.2",
        "blurb": "Runs models on this computer, free and private. Needs a fairly powerful computer, and Ollama installed.",
        "steps": ["Install Ollama from its website.", "Download a model in Ollama, for example llama3.2.", "Choose it here. No key is needed."],
    },
    {
        "id": "custom",
        "name": "Other (OpenAI-compatible)",
        "base_url": "",
        "key_prefixes": [],
        "key_url": "",
        "default_model": "",
        "blurb": "Any service with an OpenAI-compatible API. Enter its address under Advanced.",
        "steps": [],
    },
]

_BY_ID = {p["id"]: p for p in PROVIDERS}
# Values older FreeHand versions saved.
_ALIASES = {"openai_compatible": "custom", "gemini": "google", "claude": "anthropic"}



def get(provider_id: str) -> Optional[dict]:
    pid = _ALIASES.get((provider_id or "").strip().lower(), (provider_id or "").strip().lower())
    return _BY_ID.get(pid)


def identify(provider_id: str, base_url: str = "") -> str:
    """The preset for saved settings: by id, or by a matching address."""
    p = get(provider_id)
    if p and p["id"] != "custom":
        return p["id"]
    base = (base_url or "").rstrip("/").lower()
    for q in PROVIDERS:
        if q["base_url"] and q["base_url"].lower() == base:
            return q["id"]
    return p["id"] if p else "custom"


def base_url_for(provider_id: str, base_url: str = "") -> str:
    if base_url:
        return base_url.rstrip("/")
    p = get(provider_id)
    return (p or {}).get("base_url", "").rstrip("/")


def omit_temperature(provider_id: str, base_url: str = "") -> bool:
    p = get(identify(provider_id, base_url))
    return bool(p and p.get("omit_temperature"))


def public_list() -> list[dict]:
    """For the settings page."""
    keys = ("id", "name", "key_url", "blurb", "steps", "default_model")
    return [{**{k: p.get(k) for k in keys}, "signin": bool(p.get("signin")),
             "no_key": bool(p.get("no_key")), "needs_base_url": p["id"] == "custom"} for p in PROVIDERS]


def _headers(api_key: str) -> dict:
    h = {"Accept": "application/json"}
    if api_key:
        h["Authorization"] = f"Bearer {api_key}"
    return h


def list_models(provider_id: str, api_key: str, base_url: str = "", timeout: float = 8.0) -> tuple[Optional[list], str]:
    """(model ids, "") or (None, plain-language problem)."""
    base = base_url_for(provider_id, base_url)
    if not base:
        return None, "Enter the service's address under Advanced first."
    if provider_id == "google" and "{API_KEY}" in base:
        base = base.replace("{API_KEY}", api_key)
    req = urllib.request.Request(f"{base}/models", headers=_headers(api_key))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        return None, explain_http(e.code)
    except (urllib.error.URLError, OSError, ValueError):
        return None, "FreeHand couldn't reach that service. Check the internet connection, or the address under Advanced."
    items = data.get("data") if isinstance(data, dict) else data
    ids = sorted({str(m.get("id")) for m in (items or []) if isinstance(m, dict) and m.get("id")})
    # Google lists names as "models/gemini-..."; the chat API wants the bare name.
    ids = [i.split("/", 1)[1] if i.startswith("models/") else i for i in ids]
    return ids[:300], ""


def detect(api_key: str, selected: str = "") -> dict:
    """Which service is this key for, and which models does it offer?

    A key is a password: it is only ever sent to ONE service. A key whose
    start is unmistakable (sk-ant-, sk-or-, AIza, gsk_, sk-proj- ...) goes
    to that service; any other key goes only to the service chosen in the
    list (``selected``). FreeHand never tries a key at several services to
    find the right one - that would hand it to companies it doesn't belong
    to."""
    key = (api_key or "").strip()
    if not key:
        return {"error": "Paste a key first."}
    by_prefix = [p["id"] for p in PROVIDERS for pre in p["key_prefixes"] if key.startswith(pre)]
    # sk-ant- and sk-or- also start with "sk-": the longest prefix wins.
    by_prefix.sort(key=lambda pid: -max(len(x) for x in _BY_ID[pid]["key_prefixes"]))
    pid = by_prefix[0] if by_prefix else identify(selected) if selected else ""
    if not pid or pid == "custom":
        return {"error": "FreeHand can't tell which service this key is for from the key alone. "
                         "Choose the service from the list; FreeHand then checks the key with that service only."}
    models, problem = list_models(pid, key, timeout=8.0)
    out = {"provider": pid, "name": _BY_ID[pid]["name"], "models": models or [], "by_prefix": bool(by_prefix)}
    if models is None:
        out["error"] = problem
    return out


def explain_http(status: int, body: str = "") -> str:
    """A model service's error, in plain words."""
    if status in (401, 403):
        return "The key wasn't accepted. Check you copied all of it, with no spaces, and that it belongs to this service."
    if status == 402:
        return "The account needs credit. Add some on the service's billing page, then test again."
    if status == 404:
        return "The service doesn't know that model name. Choose a model from the list."
    if status == 429:
        return "The service says you've hit a limit (too many requests, or no credit left). Wait a moment, or check your credit."
    if status >= 500:
        return "The service had a problem on its side. Try again in a minute."
    detail = (body or "").strip().replace("\n", " ")[:200]
    return f"The service refused the request (error {status}){': ' + detail if detail else ''}."


def test(provider_id: str, model: str, api_key: str, base_url: str = "") -> dict:
    """Send one tiny message with these settings, before saving them."""
    p = get(identify(provider_id, base_url)) or {}
    if not model:
        return {"ok": False, "message": "Choose a model first."}
    if not api_key and not p.get("no_key"):
        return {"ok": False, "message": "Paste the key first."}
    base = base_url_for(provider_id, base_url)
    if not base:
        return {"ok": False, "message": "Enter the service's address under Advanced first."}
    if provider_id == "google" and "{API_KEY}" in base:
        base, api_key = base.replace("{API_KEY}", api_key), ""
    body = {"model": model, "max_tokens": 20,
            "messages": [{"role": "user", "content": "Reply with just the word OK."}]}
    if not p.get("omit_temperature"):
        body["temperature"] = 0
    req = urllib.request.Request(f"{base}/chat/completions", data=json.dumps(body).encode("utf-8"),
                                 headers={**_headers(api_key), "Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        try:
            body_text = e.read().decode("utf-8", "replace")
        except OSError:
            body_text = ""
        return {"ok": False, "message": explain_http(e.code, body_text)}
    except (urllib.error.URLError, OSError, ValueError):
        return {"ok": False, "message": "FreeHand couldn't reach that service. Check the internet connection, or the address under Advanced."}
    try:
        reply = (data["choices"][0]["message"].get("content") or "").strip()
    except (KeyError, IndexError, TypeError, AttributeError):
        return {"ok": False, "message": "The service answered in a way FreeHand doesn't understand. It may not be OpenAI-compatible."}
    return {"ok": True, "message": "Working. The model answered.", "reply": reply[:100]}
