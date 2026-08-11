import json
import re
import sqlite3
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, List, Optional

try:
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.interval import IntervalTrigger
    _HAS_APSCHEDULER = True
except ImportError:
    _HAS_APSCHEDULER = False

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from core.database import DB_PATH
from core.memory import sync_vault_to_sqlite

PROJECT_ROOT = Path(__file__).parent.parent
VAULT_DIR = PROJECT_ROOT / "vault"
SCRIBBLE_PATH = VAULT_DIR / "00_Scribble.md"
SWEEP_STATE_PATH = VAULT_DIR / ".sweep_state.json"
THREADS_DIR = VAULT_DIR / "Threads"

_scheduler: Optional[BackgroundScheduler] = None
DEFAULT_CRON = "0 2 * * *"
TASK_KEYWORDS = {"task", "todo", "todo:", "schedule", "reminder", "remember", "don't forget"}


def _load_settings() -> dict:
    settings_path = VAULT_DIR / "settings.json"
    if settings_path.exists():
        try:
            return json.loads(settings_path.read_text())
        except Exception:
            pass
    return {}


def _call_omniroute(text: str) -> Optional[dict]:
    settings = _load_settings()
    endpoint = settings.get("omniroute", "").strip()
    if not endpoint:
        return None
    try:
        import urllib.request
        import urllib.error
        payload = json.dumps({"text": text}).encode("utf-8")
        req = urllib.request.Request(
            endpoint,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def _classify_heuristic(text: str) -> dict:
    lower = text.lower()
    for kw in TASK_KEYWORDS:
        if kw in lower:
            return {"classification": "task", "topic": "inbox"}
    return {"classification": "thread", "topic": "general"}


def _extract_cron(entry: str) -> str:
    m = re.search(r"\[([^\]]+\*[^\]]*)\]", entry)
    if m:
        return m.group(1).strip()
    return DEFAULT_CRON


def _slugify(text: str) -> str:
    text = re.sub(r"[^a-zA-Z0-9\s-]", "", text)
    text = text.strip().replace(" ", "-").lower()
    return text[:50] or "untitled"


def _safe_thread_path(slug: str) -> Optional[Path]:
    """N4 fix: ensure slug cannot escape THREADS_DIR via traversal.

    Rejects:
    - Empty slugs
    - Slugs containing '..', '/', '\\', or other path separators after sanitisation
    - Slugs whose resolved path is not under THREADS_DIR

    Returns the safe absolute path, or None if unsafe.
    """
    if not slug or not slug.strip():
        return None
    if ".." in slug or "/" in slug or "\\" in slug:
        return None
    candidate = (THREADS_DIR / (slug + ".md")).resolve()
    threads_root = THREADS_DIR.resolve()
    try:
        # Python 3.9+: is_relative_to
        if not candidate.is_relative_to(threads_root):
            return None
    except AttributeError:
        # Fallback for older Python
        if threads_root not in candidate.parents:
            return None
    return candidate


def _read_scribble() -> str:
    if not SCRIBBLE_PATH.exists():
        return ""
    return SCRIBBLE_PATH.read_text(encoding="utf-8")


def _write_scribble(content: str) -> None:
    SCRIBBLE_PATH.write_text(content, encoding="utf-8")


def _load_sweep_state() -> dict:
    if SWEEP_STATE_PATH.exists():
        try:
            return json.loads(SWEEP_STATE_PATH.read_text())
        except Exception:
            pass
    return {"last_sweep": "", "processed_lines": []}


def _save_sweep_state(state: dict) -> None:
    SWEEP_STATE_PATH.write_text(json.dumps(state), encoding="utf-8")


def process_scribble() -> dict:
    """Read new raw entries from 00_Scribble.md, classify via OmniRoute
    (or heuristic fallback), save to SQLite or thread files, and mark
    processed entries with [x].

    Returns dict with counts: processed, tasks_created, threads_updated.
    """
    content = _read_scribble()
    if not content.strip():
        return {"processed": 0, "tasks_created": 0, "threads_updated": 0}

    lines = content.split("\n")
    state = _load_sweep_state()

    # Detect file change: if content differs from what we last processed,
    # reset the processed set so new entries are picked up.
    content_hash = hash(content)
    last_hash = state.get("content_hash", 0)
    if last_hash != 0 and content_hash != last_hash:
        processed_indices = set()
    else:
        processed_indices = set(state.get("processed_lines", []))

    new_entries: List[int] = []
    for i, line in enumerate(lines):
        if i in processed_indices:
            continue
        if re.match(r"^\s*-\s*\[\s*\]\s*", line):
            new_entries.append(i)

    if not new_entries:
        return {"processed": 0, "tasks_created": 0, "threads_updated": 0}

    tasks_created = 0
    threads_updated = 0

    for idx in new_entries:
        line = lines[idx]
        entry_text = re.sub(r"^\s*-\s*\[\s*\]\s*", "", line).strip()
        if not entry_text:
            continue

        result = _call_omniroute(entry_text)
        if result is None:
            result = _classify_heuristic(entry_text)

        classification = result.get("classification", "thread").lower()
        topic = result.get("topic", "general")

        if classification == "task":
            cron = _extract_cron(entry_text)
            title = re.sub(r"\[.+\]", "", entry_text).strip()
            if not title:
                title = entry_text[:80]
            conn = sqlite3.connect(DB_PATH)
            conn.execute(
                "INSERT INTO tasks (title, cron_schedule, status) VALUES (?, ?, ?)",
                (title, cron, "pending"),
            )
            conn.commit()
            conn.close()
            tasks_created += 1

        elif classification == "thread":
            # If the classifier returns a generic topic, derive one from the entry
            raw_topic = topic
            if raw_topic.lower() in ("general", "untitled", ""):
                raw_topic = entry_text
            topic_slug = _slugify(raw_topic)
            # N4 fix: validate the path is inside THREADS_DIR
            thread_path = _safe_thread_path(topic_slug)
            if thread_path is None:
                # Skip this entry rather than risk writing outside the vault.
                continue
            THREADS_DIR.mkdir(parents=True, exist_ok=True)

            # N8 fix: aware UTC datetime
            timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            new_block = f"\n### {timestamp}\n- {entry_text}\n"

            if thread_path.exists():
                existing = thread_path.read_text(encoding="utf-8")
                thread_path.write_text(existing + new_block, encoding="utf-8")
            else:
                thread_path.write_text(
                    f"# {topic}\n\n> Auto-captured thread note\n",
                    encoding="utf-8",
                )
                thread_path.write_text(
                    thread_path.read_text(encoding="utf-8") + new_block,
                    encoding="utf-8",
                )
            threads_updated += 1

        lines[idx] = re.sub(r"^\s*-\s*\[\s*\]\s*", "- [x] ", lines[idx])
        processed_indices.add(idx)

    _write_scribble("\n".join(lines))
    # N8 fix: aware UTC datetime
    now_iso = datetime.now(timezone.utc).isoformat()
    _save_sweep_state({
        "last_sweep": now_iso,
        "processed_lines": sorted(processed_indices),
        "content_hash": content_hash,
    })

    return {
        "processed": len(new_entries),
        "tasks_created": tasks_created,
        "threads_updated": threads_updated,
    }


def get_scheduler() -> Optional[BackgroundScheduler]:
    """Return the singleton BackgroundScheduler, starting it if needed.
    Returns None if APScheduler is not installed.
    """
    global _scheduler
    if not _HAS_APSCHEDULER:
        return None
    if _scheduler is None:
        _scheduler = BackgroundScheduler()
        interval = _load_settings().get("sweep_interval", 30)
        _scheduler.add_job(
            process_scribble,
            IntervalTrigger(seconds=int(interval)),
            id="scribble_sweep",
            name="Scribble Sweep",
            replace_existing=True,
        )
        _scheduler.start()
    return _scheduler


def shutdown_scheduler() -> None:
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        _scheduler.shutdown(wait=False)
        _scheduler = None
