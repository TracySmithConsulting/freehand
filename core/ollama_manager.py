"""Models on this computer, through Ollama (Nov 2026).

A friendly manager for people who want a free, private model that runs on
their own computer:

1. **Is Ollama there?** FreeHand (in Docker) asks Ollama on the computer
   (host.docker.internal:11434) and lists the models already downloaded.
2. **What can this computer run?** FreeHand can't see the graphics card
   from inside Docker, so ``scripts/check-computer.ps1`` looks once and
   saves ``vault/computer.json``.
3. **Recommend** models that can use tools (FreeHand needs that) and fit:
   on the graphics card if there's room, otherwise small enough for the
   processor. Sizes are checked with Ollama's own library.
4. **Download** with progress, then test it and use it as the main model
   or a backup.

Installing Ollama itself happens on the computer, not in Docker: the
console links to ollama.com/download, and check-computer.ps1 can install
it with winget (-InstallOllama).
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from typing import Optional

# Models that can use tools, smallest first: (name, download GB if the
# library can't be reached, what it's like).
CANDIDATES = (
    ("qwen3:1.7b", 1.4, "Very small and quick. Fine for simple chats, weak at harder tasks."),
    ("llama3.2:3b", 2.0, "Small and quick, from Meta. Good for everyday chats."),
    ("qwen3:4b", 2.5, "Small, and the best at using tools for its size."),
    ("qwen3:8b", 5.2, "Noticeably smarter, needs more room."),
    ("qwen3:14b", 9.3, "Smart, needs a strong graphics card."),
    ("gpt-oss:20b", 13.8, "OpenAI's open model. Needs a powerful computer."),
    ("qwen3:32b", 20.2, "Very capable. Needs a top graphics card."),
)

_pull = {"model": "", "status": "", "completed": 0, "total": 0, "done": False, "error": "", "started": 0.0}
_pull_lock = threading.Lock()
_size_cache: dict[str, float] = {}


DEFAULT_PORT = 11434


def base_url() -> str:
    """Ollama's own API (not the /v1 one used for chatting).

    Usually port 11434, but OLLAMA_HOST can move Ollama (this computer uses
    11435), so the port check-computer.ps1 found wins.
    """
    from core import llm_providers
    url = (llm_providers.get("ollama") or {}).get("base_url", "http://host.docker.internal:11434/v1")
    url = url.rstrip("/").removesuffix("/v1")
    port = (computer() or {}).get("ollama_port")
    try:
        port = int(port)
    except (TypeError, ValueError):
        port = DEFAULT_PORT
    if port != DEFAULT_PORT:
        url = url.replace(f":{DEFAULT_PORT}", f":{port}")
    return url


def chat_base_url() -> str:
    """What to save as the model's address: empty for the usual port."""
    url = base_url()
    return "" if url.endswith(f":{DEFAULT_PORT}") else url + "/v1"


def _call(method: str, path: str, body: Optional[dict] = None, timeout: float = 5.0):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(base_url() + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"} if data else {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        return e.code, None
    except (urllib.error.URLError, OSError, ValueError):
        return None, None


def computer() -> Optional[dict]:
    """What scripts/check-computer.ps1 found, or None if it hasn't run."""
    from core import agent_config
    path = agent_config.VAULT_DIR / "computer.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _best_gpu(info: dict) -> Optional[dict]:
    """The graphics card Ollama would use: a dedicated NVIDIA or AMD one.
    Built-in Intel graphics share the computer's memory and don't help."""
    gpus = [g for g in (info or {}).get("gpus") or [] if g.get("vendor") in ("nvidia", "amd")]
    return max(gpus, key=lambda g: float(g.get("vram_gb") or 0), default=None)


def download_size(model: str, fallback: float) -> float:
    """Download size in GB, from Ollama's library (cached)."""
    if model in _size_cache:
        return _size_cache[model]
    name, _, tag = model.partition(":")
    req = urllib.request.Request(
        f"https://registry.ollama.ai/v2/library/{name}/manifests/{tag or 'latest'}",
        headers={"Accept": "application/vnd.docker.distribution.manifest.v2+json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            manifest = json.loads(resp.read())
        size = round(sum(int(layer.get("size") or 0) for layer in manifest.get("layers") or []) / 1e9, 1)
        _size_cache[model] = size or fallback
    except (urllib.error.URLError, OSError, ValueError):
        return fallback
    return _size_cache[model]


def ollama_recommendations() -> list:
    """Ollama's own suggestions (newer Ollama), models that run here only.

    Each has the video memory it needs. Cloud models (":cloud") are left
    out: they run on Ollama's servers, which is what Ollama Cloud is for.
    """
    code, data = _call("GET", "/api/experimental/model-recommendations", timeout=5.0)
    if code != 200 or not isinstance(data, dict):
        return []
    out = []
    for r in data.get("recommendations") or []:
        name = str(r.get("model") or "")
        if not name or name.endswith(":cloud") or not r.get("vram_bytes"):
            continue
        out.append({"model": name, "needs_gb": round(int(r["vram_bytes"]) / 1e9, 1),
                    "blurb": str(r.get("description") or "").strip()})
    return out


def recommend(info: Optional[dict], from_ollama: Optional[list] = None) -> dict:
    """Up to three models that fit this computer, best fit first.

    Ollama's own suggestions come first when it has any; FreeHand's list
    (CANDIDATES) fills in for older Ollama or when Ollama isn't running.
    """
    if not info:
        return {"known": False, "models": []}
    ram = float(info.get("ram_gb") or 0)
    gpu = _best_gpu(info)
    vram = float(gpu.get("vram_gb") or 0) if gpu else 0.0
    rows = []
    for r in from_ollama or []:
        if vram and r["needs_gb"] <= vram + 0.1:
            runs_on = "graphics card"
        elif r["needs_gb"] <= 4.0 and r["needs_gb"] + 4.0 <= ram * 0.75:
            runs_on = "processor"
        else:
            continue
        rows.append({"model": r["model"], "download_gb": download_size(r["model"], r["needs_gb"]),
                     "blurb": r["blurb"] + ".", "runs_on": runs_on, "source": "ollama"})
    if rows:
        best = rows[:3]
        best[0]["best"] = True
        return {"known": True, "summary": _summary(gpu, vram, ram), "models": best}
    for name, fallback, blurb in CANDIDATES:
        size = download_size(name, fallback)
        on_gpu = vram and size + 1.0 <= vram
        # On the processor alone, models need room beside everything else,
        # and anything over ~4B parameters is painfully slow.
        on_cpu = size + 4.0 <= ram * 0.75 and size <= 3.0
        if on_gpu or on_cpu:
            rows.append({"model": name, "download_gb": size, "blurb": blurb,
                         "runs_on": "graphics card" if on_gpu else "processor"})
    rows.reverse()  # biggest that fits first
    best = rows[:3]
    if best:
        best[0]["best"] = True
    return {"known": True, "summary": _summary(gpu, vram, ram), "models": best}


def _summary(gpu: Optional[dict], vram: float, ram: float) -> str:
    if gpu:
        return f"{gpu.get('name')} with {vram:g} GB, and {ram:g} GB of memory"
    return f"{ram:g} GB of memory and no graphics card Ollama can use, so models run on the processor (slower)"


def status() -> dict:
    """Is Ollama running, and which models are already on this computer?"""
    code, version = _call("GET", "/api/version", timeout=3.0)
    info = computer()
    out = {"running": code == 200, "version": (version or {}).get("version", ""), "models": [],
           "computer": info, "ollama_installed": (info or {}).get("ollama_installed"),
           "chat_base_url": chat_base_url()}
    if code == 200:
        _, tags = _call("GET", "/api/tags", timeout=5.0)
        for m in (tags or {}).get("models") or []:
            name = m.get("name") or m.get("model") or ""
            _, shown = _call("POST", "/api/show", {"model": name}, timeout=10.0)
            caps = (shown or {}).get("capabilities") or []
            out["models"].append({"model": name, "size_gb": round(int(m.get("size") or 0) / 1e9, 1),
                                  "tools": "tools" in caps})
    out["recommended"] = recommend(info, ollama_recommendations() if code == 200 else None)
    have = {m["model"] for m in out["models"]}
    for r in out["recommended"]["models"]:
        r["downloaded"] = r["model"] in have
    return out


# ── Downloading (one at a time) ──────────────────────────────────────

def pull_status() -> dict:
    with _pull_lock:
        return dict(_pull)


def start_pull(model: str) -> dict:
    model = (model or "").strip()
    if not model or any(c.isspace() for c in model) or len(model) > 100:
        return {"ok": False, "message": "Choose a model to download."}
    with _pull_lock:
        if _pull["model"] and not _pull["done"]:
            return {"ok": False, "message": f"{_pull['model']} is still downloading. Wait for it to finish."}
        _pull.update(model=model, status="Starting...", completed=0, total=0, done=False, error="",
                     started=time.time())
    threading.Thread(target=_pull_worker, args=(model,), daemon=True).start()
    return {"ok": True, "message": f"Downloading {model}."}


def _pull_worker(model: str) -> None:
    req = urllib.request.Request(base_url() + "/api/pull", method="POST",
                                 data=json.dumps({"model": model, "stream": True}).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60 * 60 * 3) as resp:
            for raw in resp:
                try:
                    line = json.loads(raw.decode("utf-8", "replace") or "{}")
                except ValueError:
                    continue
                with _pull_lock:
                    if line.get("error"):
                        _pull.update(error=str(line["error"]), done=True)
                        return
                    _pull["status"] = line.get("status", _pull["status"])
                    if line.get("total"):
                        _pull["total"] = int(line["total"])
                        _pull["completed"] = int(line.get("completed") or 0)
        with _pull_lock:
            _pull.update(status="success", done=True, completed=_pull["total"] or _pull["completed"])
    except (urllib.error.URLError, OSError) as e:
        with _pull_lock:
            _pull.update(done=True, error="Ollama stopped answering during the download. "
                                          "Check it's still running, then try again." if "timed out" not in str(e)
                                          else "The download took too long. Try again.")


def use(model: str, as_what: str) -> dict:
    """Make a downloaded model the main one or a backup."""
    from core import agent_config
    if not model:
        return {"ok": False, "message": "Choose a model first."}
    settings = {"provider": "ollama", "model": model, "api_key": "", "base_url": chat_base_url()}
    if as_what == "backup":
        agent_config.add_llm_backup(settings)
        return {"ok": True, "message": f"Added {model} on this computer as a backup."}
    current = agent_config.get_llm_config()
    if agent_config.llm_is_configured(current) and current.get("provider") != "ollama":
        # Keep the model people had: it becomes the first backup.
        agent_config._save_llm_backups([{k: current.get(k, "") for k in ("provider", "model", "api_key", "base_url")},
                                        *agent_config.get_llm_backups()])
        note = " Your previous main model is now the first backup."
    else:
        note = ""
    agent_config.set_llm_config(settings)
    return {"ok": True, "message": f"FreeHand now uses {model} on this computer.{note}"}
