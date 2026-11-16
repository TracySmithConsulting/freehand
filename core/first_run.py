"""The first-run setup (step 2 of docs/design/granny-installer.md).

A new FreeHand has no AI model, and a person who's never used one doesn't
know what a model, a provider or a key is. The setup asks one thing at a
time: their name, how FreeHand should think (a free account, a paid one,
this computer, or a key they were given), and optionally a second free
account as a backup. Then it suggests a first question.

Each key is tested for real before it's kept, and a key is only ever sent
to the one service it belongs to (core/llm_providers.detect).
"""
from __future__ import annotations

from typing import Optional

# Models to prefer, in order, when a service offers several. Free lists
# change (Groq dropped Llama from its free tier in 2026), so FreeHand picks
# from what the account actually offers, then tests it.
PREFERRED = {
    "groq": ["openai/gpt-oss-120b", "openai/gpt-oss-20b", "qwen/qwen3-32b", "llama-3.3-70b-versatile"],
    "nvidia": ["meta/llama-3.3-70b-instruct", "nvidia/llama-3.3-nemotron-super-49b-v1.5",
               "mistralai/mistral-nemotron", "qwen/qwen3-next-80b-a3b-instruct", "meta/llama-3.1-70b-instruct"],
    "google": ["gemini-flash-lite-latest", "gemini-flash-latest", "gemini-2.5-flash-lite", "gemini-2.5-flash"],
    "openrouter": ["openrouter/free"],
}

FREE_OPENROUTER_MODEL = "openrouter/free"


def _settings() -> dict:
    from core.agent_config import _load_settings
    return _load_settings() or {}


def _save(key: str, value) -> None:
    from core.agent_config import _load_settings, _save_settings
    data = _load_settings() or {}
    data[key] = value
    _save_settings(data)


def name() -> str:
    user = _settings().get("user") or {}
    return str(user.get("name") or "").strip() if isinstance(user, dict) else ""


def set_name(value: str) -> str:
    value = " ".join(str(value or "").split())[:60]
    user = _settings().get("user")
    user = dict(user) if isinstance(user, dict) else {}
    user["name"] = value
    _save("user", user)
    return value


def status() -> dict:
    """What the setup page needs to know: is a model set up yet, and what's done."""
    from core import llm_providers
    from core.agent_config import get_llm_backups, get_llm_config, llm_is_configured
    main = get_llm_config()
    has_model = llm_is_configured(main)
    out = {
        "needed": not has_model,
        "done": bool(_settings().get("setup_done")),
        "name": name(),
        "backups": len(get_llm_backups()),
    }
    if has_model:
        p = llm_providers.get(llm_providers.identify(main.get("provider", ""), main.get("base_url", ""))) or {}
        out["model"] = {"service": p.get("name") or main.get("provider", ""), "model": main.get("model", "")}
    try:
        from core.oauth import open_connector
        out["email_connected"] = any(a.get("service") in ("gmail", "outlook") for a in open_connector.list_accounts())
    except Exception:
        out["email_connected"] = False
    return out


def finish() -> None:
    _save("setup_done", True)


def choose_model(provider: str, models: list) -> str:
    """The model to use from what this account offers."""
    from core import llm_providers
    offered = [m.get("id") if isinstance(m, dict) else str(m) for m in models or []]
    for want in PREFERRED.get(provider, []):
        if want in offered:
            return want
    default = (llm_providers.get(provider) or {}).get("default_model", "")
    if default and (not offered or default in offered):
        return default
    instruct = [m for m in offered if "instruct" in m.lower() or "chat" in m.lower()]
    return (instruct or offered or [default])[0]


def use_key(key: str, provider: str = "", role: str = "main", free: bool = False) -> dict:
    """Work out the service, choose a model, test it, and keep it as the
    main model or a backup. Plain words either way."""
    from core import llm_providers
    from core.agent_config import add_llm_backup, get_llm_backups, set_llm_config
    found = llm_providers.detect(key, provider)
    if found.get("error"):
        return {"ok": False, "message": found["error"]}
    pid = found["provider"]
    # OpenRouter lists hundreds of models; for a free start, its free router.
    model = FREE_OPENROUTER_MODEL if pid == "openrouter" and free else choose_model(pid, found.get("models"))
    result = llm_providers.test(pid, model, key.strip(), "")
    if not result.get("ok"):
        return {"ok": False, "message": f"{found['name']} didn't answer with that key: {result.get('message')}"}
    config = {"provider": pid, "model": model, "api_key": key.strip(), "base_url": ""}
    if role == "backup":
        if any(b.get("api_key") == config["api_key"] and b.get("model") == model for b in get_llm_backups()):
            return {"ok": True, "message": f"{found['name']} is already a backup.", "service": found["name"], "model": model}
        add_llm_backup(config)
        where = "as a backup. When your main model runs out or doesn't answer, FreeHand uses it instead."
    else:
        set_llm_config(config)
        where = "as your AI model."
    return {"ok": True, "service": found["name"], "model": model,
            "message": f"{found['name']} works, with the model {model}. FreeHand will use it {where}"}


def use_ollama(model: str = "") -> dict:
    """Ollama on this computer: no key, but it must be running with a model."""
    from core import llm_providers
    from core.agent_config import set_llm_config
    models, problem = llm_providers.list_models("ollama", "", timeout=5.0)
    if models is None:
        return {"ok": False, "message": "FreeHand can't find Ollama on this computer. Install it from ollama.com, "
                                        "start it, then try again."}
    if not models:
        return {"ok": False, "message": "Ollama is running, but has no models yet. In Ollama, download one "
                                        "(for example llama3.2), then try again."}
    names = [m.get("id") if isinstance(m, dict) else str(m) for m in models]
    pick = model if model in names else names[0]
    result = llm_providers.test("ollama", pick, "", "")
    if not result.get("ok"):
        return {"ok": False, "message": f"Ollama didn't answer with {pick}: {result.get('message')}"}
    set_llm_config({"provider": "ollama", "model": pick, "api_key": "", "base_url": ""})
    return {"ok": True, "service": "Ollama", "model": pick,
            "message": f"Ollama works, with {pick}. FreeHand will think on this computer: free and private."}


def prompt_line() -> str:
    """For FreeHand's instructions, so it can greet the person and sign
    emails with their name."""
    who = name()
    return f"The person's name is {who}.\n" if who else ""


def provider_for_ui(pid: str) -> Optional[dict]:
    from core import llm_providers
    p = llm_providers.get(pid)
    if not p:
        return None
    return {"id": p["id"], "name": p["name"], "key_url": p.get("key_url", ""), "steps": p.get("steps", []),
            "blurb": p.get("blurb", "")}
