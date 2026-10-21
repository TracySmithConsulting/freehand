from __future__ import annotations
import asyncio
import hashlib
import json
import re
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

try:
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.interval import IntervalTrigger
    _HAS_APSCHEDULER = True
except ImportError:
    _HAS_APSCHEDULER = False

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from core.memory import sync_vault_to_sqlite
# Reference DB_PATH through the module (not a from-import) so tests can
# monkeypatch core.database.DB_PATH and have scheduler/gateway calls follow.
import core.database as database
from core.agent_config import get_llm_config

PROJECT_ROOT = Path(__file__).parent.parent
VAULT_DIR = PROJECT_ROOT / "vault"
SCRIBBLE_PATH = VAULT_DIR / "00_Scribble.md"
SWEEP_STATE_PATH = VAULT_DIR / ".sweep_state.json"
THREADS_DIR = VAULT_DIR / "Threads"

_scheduler: Optional[BackgroundScheduler] = None
DEFAULT_CRON = "0 2 * * *"
TASK_KEYWORDS = {"task", "todo", "todo:", "schedule", "reminder", "remember", "don't forget"}

# Single in-process writer lock for 00_Scribble.md. The sweep rewrites the
# whole file; gateway captures and UI appends go through append_scribble().
# This serialises the writers within one process (the historical race was
# sweep-rewrite vs concurrent append losing the appended line).
_SCRIBBLE_LOCK = threading.Lock()
_TASK_RUNNER_WARNED = False

_CLASSIFY_SYSTEM_PROMPT = (
    "You classify a short captured note. Respond with ONLY a JSON object, no prose: "
    '{"classification": "task" or "note", '
    '"topic": "very short topic label, 2-4 words, or empty if unclear", '
    '"cron": "5-field cron expression ONLY if the note clearly implies a schedule, otherwise empty string"}'
)


def _run_coro(coro):
    """Run a coroutine whether or not we're already inside an event loop.

    The sweep can be triggered from APScheduler threads (no loop), from the
    CLI (no loop), or from async FastAPI endpoints like /api/sweep and the
    gateway /sweep command (running loop). asyncio.run() raises in the last
    case, so execute in a worker thread there instead.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


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


def _classify_via_llm(text: str) -> Optional[dict]:
    """Classify a scribble entry using the LLM configured in settings.llm.

    Returns {"classification": "task"|"note", "topic": str, "cron": str},
    or None when no LLM is configured, the call fails, or the reply is
    unparseable — the caller then falls back to the keyword heuristic.
    """
    try:
        config = get_llm_config()
        if not (config.get("api_key", "").strip() or config.get("base_url", "").strip()):
            return None
        from core.agent import call_llm
        resp = _run_coro(call_llm([
            {"role": "system", "content": _CLASSIFY_SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ], config=config))
        if "error" in resp:
            return None
        content = (resp.get("content") or "").strip()
        # Strip a markdown fence if the model added one.
        content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip())
        data = json.loads(content)
        if not isinstance(data, dict):
            return None
        classification = str(data.get("classification", "")).strip().lower()
        if classification not in ("task", "note"):
            return None
        return {
            "classification": classification,
            "topic": str(data.get("topic", "")).strip()[:80],
            "cron": str(data.get("cron", "")).strip(),
        }
    except Exception:
        return None


def _classify_heuristic(text: str) -> dict:
    lower = text.lower()
    for kw in TASK_KEYWORDS:
        if kw in lower:
            return {"classification": "task", "topic": "inbox"}
    return {"classification": "thread", "topic": "general"}


def _classify_entry(text: str) -> dict:
    """OmniRoute endpoint (if set) → configured LLM → keyword heuristic."""
    result = _call_omniroute(text)
    if result is None:
        result = _classify_via_llm(text)
    if result is None:
        result = _classify_heuristic(text)
    return result


# ──────────────────────────────────────────────────────────────────────
# Cron parsing / validation
# ──────────────────────────────────────────────────────────────────────

def _parse_cron_field(field: str, lo: int, hi: int) -> Optional[frozenset]:
    """Parse one cron field into the set of values it matches.

    Supports: number, list (a,b), range (a-b), step (*/n, a-b/n, *//n not
    accepted), and '*'. Returns None on any malformed or out-of-range token.
    """
    field = field.strip()
    if not field or field == "?":
        return None
    values: set = set()
    for part in field.split(","):
        part = part.strip()
        step = 1
        if "/" in part:
            base, _, step_s = part.partition("/")
            if not step_s.isdigit() or int(step_s) <= 0:
                return None
            step = int(step_s)
            part = base
        if part == "*" or part == "":
            rng = range(lo, hi + 1, step)
        else:
            m = re.fullmatch(r"(\d+)(?:-(\d+))?", part)
            if not m:
                return None
            a = int(m.group(1))
            b = int(m.group(2)) if m.group(2) else a
            if a < lo or b > hi or a > b:
                # allow a > b wrap ranges? Standard cron does not — reject.
                return None
            rng = range(a, b + 1, step)
        values.update(rng)
    if not values:
        return None
    return frozenset(values)


def _parse_cron(expr: str) -> Optional[dict]:
    """Validate a 5-field cron expression. Returns field sets or None."""
    parts = expr.split()
    if len(parts) != 5:
        return None
    minute = _parse_cron_field(parts[0], 0, 59)
    hour = _parse_cron_field(parts[1], 0, 23)
    dom = _parse_cron_field(parts[2], 1, 31)
    month = _parse_cron_field(parts[3], 1, 12)
    dow = _parse_cron_field(parts[4], 0, 7)
    if None in (minute, hour, dom, month, dow):
        return None
    # Cron convention: 0 and 7 both mean Sunday.
    dow_vals = set(dow)
    if 7 in dow_vals:
        dow_vals.discard(7)
        dow_vals.add(0)
    return {
        "minute": minute,
        "hour": hour,
        "dom": dom,
        "dom_wild": parts[2].strip() in ("*", ""),
        "month": month,
        "dow": frozenset(dow_vals),
        "dow_wild": parts[4].strip() in ("*", ""),
    }


def _cron_next(expr: str, after: Optional[datetime] = None) -> Optional[datetime]:
    """Next UTC datetime matching a 5-field cron expression, or None.

    Cron is interpreted in UTC (this codebase's canonical clock). Brute-force
    minute stepping capped at 366 days; expressions that never fire inside a
    year (e.g. '0 0 30 2 *') return None.
    """
    fields = _parse_cron(expr)
    if fields is None:
        return None
    start = after or datetime.now(timezone.utc)
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    dt = start.astimezone(timezone.utc)
    dt = dt.replace(second=0, microsecond=0) + timedelta(minutes=1)
    for _ in range(366 * 1440):
        if (
            dt.minute in fields["minute"]
            and dt.hour in fields["hour"]
            and dt.month in fields["month"]
        ):
            cron_dow = (dt.weekday() + 1) % 7  # Python Monday=0 → cron Sunday=0
            dom_ok = dt.day in fields["dom"]
            dow_ok = cron_dow in fields["dow"]
            if fields["dom_wild"] and fields["dow_wild"]:
                day_ok = True
            elif fields["dom_wild"]:
                day_ok = dow_ok
            elif fields["dow_wild"]:
                day_ok = dom_ok
            else:
                day_ok = dom_ok or dow_ok  # standard cron OR when both restricted
            if day_ok:
                return dt
        dt += timedelta(minutes=1)
    return None


def _extract_cron(entry: str) -> str:
    """Extract a bracketed cron-like schedule if (and only if) it is a
    valid 5-field cron expression. Anything else — e.g. the note '[wip*]' —
    returns '' so the caller falls back to DEFAULT_CRON instead of storing
    garbage as a schedule.
    """
    m = re.search(r"\[([^\]]+\*[^\]]*)\]", entry)
    if m:
        candidate = m.group(1).strip()
        if _parse_cron(candidate) is not None:
            return candidate
    return ""


def _iso_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


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


def append_scribble(content: str) -> str:
    """Append an entry to 00_Scribble.md in the sweepable checklist format.

    The single writer entry point for non-sweep producers (gateway /scribble,
    remote captures). Lock-guarded against the sweep's rewrite.
    """
    content = (content or "").strip()
    if not content:
        return "Use: /scribble <your note>"
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    line = f"- [ ] [{timestamp}] {content}\n"
    with _SCRIBBLE_LOCK:
        existing = _read_scribble()
        new_content = (existing.rstrip() + "\n" if existing.strip() else "") + line
        _write_scribble(new_content)
    return f"Added to Scribble: {content[:80]}"


def _load_sweep_state() -> dict:
    if SWEEP_STATE_PATH.exists():
        try:
            return json.loads(SWEEP_STATE_PATH.read_text())
        except Exception:
            pass
    return {"last_sweep": "", "processed_lines": []}


def _save_sweep_state(state: dict) -> None:
    SWEEP_STATE_PATH.write_text(json.dumps(state), encoding="utf-8")


def _append_journal(entry: str) -> None:
    """Append a timestamped line to vault/Journal/YYYY-MM.md.

    Journal files are plain .md inside the vault, so sync_vault_to_sqlite
    indexes them — archived history is searchable memory, not dead weight.
    """
    now = datetime.now(timezone.utc)
    journal_dir = VAULT_DIR / "Journal"
    journal_dir.mkdir(parents=True, exist_ok=True)
    path = journal_dir / (now.strftime("%Y-%m") + ".md")
    line = "\n- " + entry + "\n"
    with _SCRIBBLE_LOCK:
        if path.exists():
            existing = path.read_text(encoding="utf-8")
            path.write_text(existing.rstrip("\n") + line, encoding="utf-8")
        else:
            path.write_text(
                f"# Journal {now.strftime('%Y-%m')}\n{line}",
                encoding="utf-8",
            )


def _append_thread(raw_topic: str, entry_text: str) -> int:
    """Append an entry block to its topic thread file. Returns 1 on success.
    Creates the file (with a proper header) when needed; never double-writes."""
    now = datetime.now(timezone.utc)
    timestamp = now.strftime("%Y-%m-%d %H:%M UTC")
    new_block = f"\n### {timestamp}\n- {entry_text}\n"
    if not THREADS_DIR.exists():
        THREADS_DIR.mkdir(parents=True, exist_ok=True)
    slug = _slugify(raw_topic or entry_text)
    thread_path = _safe_thread_path(slug)
    if thread_path is None:
        return 0
    if thread_path.exists():
        existing = thread_path.read_text(encoding="utf-8")
        thread_path.write_text(
            existing.rstrip("\n") + new_block, encoding="utf-8"
        )
    else:
        thread_path.write_text(
            f"# {raw_topic}\n\n> Auto-captured thread note\n{new_block}",
            encoding="utf-8",
        )
    return 1


# ──────────────────────────────────────────────────────────────────────
# Task creation + execution
# ──────────────────────────────────────────────────────────────────────

def _tasks_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(database.DB_PATH))
    missing = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='tasks'"
    ).fetchone() is None
    if missing:
        conn.close()
        database.init_db()
        conn = sqlite3.connect(str(database.DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def create_task(title: str, cron_schedule: str) -> dict:
    """Insert a task row and compute its first scheduled run.

    A valid 5-field cron makes the task recurring (next_run set; the runner
    re-computes after each run). An empty or invalid cron makes it one-shot
    (next_run stays NULL; the runner runs it once, then marks it done).
    Never raises in normal operation; DB failures surface to the caller.
    """
    title = (title or "").strip()
    cron = (cron_schedule or "").strip() or DEFAULT_CRON
    next_run = _cron_next(cron)
    conn = _tasks_conn()
    try:
        cursor = conn.execute(
            "INSERT INTO tasks (title, cron_schedule, status, next_run, last_run) "
            "VALUES (?, ?, 'pending', ?, NULL)",
            (title, cron, _iso_utc(next_run) if next_run else None),
        )
        conn.commit()
        task_id = cursor.lastrowid
        return {
            "id": task_id,
            "title": title,
            "cron_schedule": cron,
            "status": "pending",
            "next_run": _iso_utc(next_run) if next_run else None,
            "recurring": next_run is not None,
        }
    finally:
        conn.close()


def backfill_next_run() -> dict:
    """Repair pre-R16 task rows from older UI/API code paths.

    Older FreeHand versions inserted task rows without ever computing
    next_run, so a row could sit pending forever with a valid cron (and
    duplicated itself when the same entry was captured twice). On boot:

    - Deduplicate pending rows that share (title, cron_schedule) AND have
      a NULL next_run — the signature legacy duplicates have (R17-P2).
      Successfully scheduled duplicates are left alone.
    - Backfill next_run for pending NULL-next_run rows whose cron parses
      and fires — they become properly scheduled recurring tasks.
      Unparseable crons are left alone: they stay honest one-shots.

    Returns {"deduped": n, "backfilled": n}.
    """
    conn = _tasks_conn()
    deduped = 0
    backfilled = 0
    try:
        rows = conn.execute(
            "SELECT id, title, cron_schedule FROM tasks "
            "WHERE status = 'pending' AND next_run IS NULL ORDER BY id"
        ).fetchall()

        # Dedupe: first row seen per (title, cron) is the survivor.
        survivors = set()
        for row in rows:
            key = (row["title"], row["cron_schedule"])
            if key in survivors:
                conn.execute(
                    "DELETE FROM tasks WHERE id = ?", (row["id"],)
                )
                deduped += 1
            else:
                survivors.add(key)

        # Backfill: any remaining NULL-next_run row with a fireable cron
        # gets its next run computed. (Rows just deleted can't match —
        # recompute from scratch for simplicity and lock-free safety.)
        remaining = conn.execute(
            "SELECT id, cron_schedule FROM tasks "
            "WHERE status = 'pending' AND next_run IS NULL"
        ).fetchall()
        for row in remaining:
            nxt = _cron_next(row["cron_schedule"])
            if nxt is not None:
                conn.execute(
                    "UPDATE tasks SET next_run = ? WHERE id = ?",
                    (_iso_utc(nxt), row["id"]),
                )
                backfilled += 1
        conn.commit()
    finally:
        conn.close()
    return {"deduped": deduped, "backfilled": backfilled}


def run_due_tasks(now: Optional[datetime] = None) -> dict:
    """Execute pending tasks whose next_run has arrived (or one-shot rows
    with no next_run). Oldest task id first, capped per tick
    (settings.task_max_per_tick, default 3) so a pile due at the same cron
    moment doesn't thunder-herd the LLM.

    Recurring tasks get their next_run recomputed; one-shot tasks are marked
    'done' after a successful run. On execution failure the task stays
    pending and its last_run records the error.

    Returns {"executed": n, "failed": n, "skipped": reason|None}.
    """
    global _TASK_RUNNER_WARNED
    from core.agent import run_agent  # lazy: avoids the heavy import chain at module load

    config = get_llm_config()
    if not (config.get("api_key", "").strip() or config.get("base_url", "").strip()):
        if not _TASK_RUNNER_WARNED:
            print("[WARN] No LLM configured (settings.llm) — task runner is idle. "
                  "Set an API key in the web UI to enable scheduled task execution.")
            _TASK_RUNNER_WARNED = True
        return {"executed": 0, "failed": 0, "skipped": "llm_not_configured"}

    cap = int(_load_settings().get("task_max_per_tick", 3))
    cap = max(1, cap)
    now = now or datetime.now(timezone.utc)
    now_iso = _iso_utc(now)
    conn = _tasks_conn()
    executed = 0
    failed = 0
    try:
        rows = conn.execute(
            "SELECT id, title, cron_schedule, next_run FROM tasks "
            "WHERE status = 'pending' AND (next_run IS NULL OR next_run <= ?) "
            "ORDER BY id LIMIT ?",
            (now_iso, cap),
        ).fetchall()
        for row in rows:
            result = _run_coro(run_agent(row["title"], max_turns=3, source="cli"))
            error = result.get("error")
            stamp = _iso_utc(datetime.now(timezone.utc))
            err_note = (stamp + " error: " + str(error)[:120]) if error else stamp
            next_iso = None
            if row["next_run"]:
                nxt = _cron_next(row["cron_schedule"], after=now)
                next_iso = _iso_utc(nxt) if nxt else None
            if error:
                failed += 1
                conn.execute(
                    "UPDATE tasks SET last_run = ?, next_run = ? WHERE id = ?",
                    (err_note, next_iso, row["id"]),
                )
            else:
                executed += 1
                new_status = "pending" if next_iso else "done"
                conn.execute(
                    "UPDATE tasks SET last_run = ?, next_run = ?, status = ? WHERE id = ?",
                    (stamp, next_iso, new_status, row["id"]),
                )
        conn.commit()
    finally:
        conn.close()
    return {"executed": executed, "failed": failed, "skipped": None}


# ──────────────────────────────────────────────────────────────────────
# The sweep
# ──────────────────────────────────────────────────────────────────────

def process_scribble() -> dict:
    """Read new raw entries from 00_Scribble.md, classify them (OmniRoute →
    configured LLM → keyword heuristic), and place each entry:

    - task     → tasks table (via create_task, second-run validated cron)
                 + an audit line in Journal/YYYY-MM.md
    - note     → its topic thread file in Threads/<topic>.md

    Consumed entries are then removed from the pad — as are legacy '- [x]'
    lines from earlier sweep versions (archived into Journal, which FTS5
    indexes). The pad stays small; history stays searchable.

    Returns dict with counts: processed, tasks_created, threads_updated,
    archived.
    """
    content = _read_scribble()
    if not content.strip():
        return {"processed": 0, "tasks_created": 0, "threads_updated": 0, "archived": 0}

    lines = content.split("\n")
    state = _load_sweep_state()

    # Detect file change: if content differs from what we last processed,
    # reset the processed set so new entries are picked up.
    # Stable hash (sha256) — Python's salted str hash changed every restart.
    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    last_hash = state.get("content_hash")
    if last_hash and content_hash != last_hash:
        processed_indices = set()
    else:
        processed_indices = set(state.get("processed_lines", []))

    unchecked_re = re.compile(r"^\s*-\s*\[\s*\]\s*")
    done_re = re.compile(r"^\s*-\s*\[\s*[xX]\s*\]")  # legacy processed lines
    new_entries: List[int] = []
    legacy_done: List[int] = []
    for i, line in enumerate(lines):
        if unchecked_re.match(line):
            if i not in processed_indices:
                new_entries.append(i)
        elif done_re.match(line):
            legacy_done.append(i)

    tasks_created = 0
    threads_updated = 0
    archived = 0

    for idx in new_entries:
        line = lines[idx]
        entry_text = re.sub(r"^\s*-\s*\[\s*\]\s*", "", line).strip()
        if not entry_text:
            continue

        result = _classify_entry(entry_text)

        classification = result.get("classification", "thread").lower()
        topic = result.get("topic", "general")

        if classification == "task":
            cron = result.get("cron", "") or _extract_cron(entry_text)
            title = re.sub(r"\[[^\]]*\]", "", entry_text).strip()
            if not title:
                title = entry_text[:80]
            created = create_task(title, cron or DEFAULT_CRON)
            tasks_created += 1
            sched_desc = (
                f"cron {created['cron_schedule']}"
                if created["recurring"]
                else "one-shot (cron could not be parsed)"
            )
            _append_journal(f"task scheduled — {title} ({sched_desc})")
            archived += 1

        else:
            # If the classifier returns a generic topic, derive one from the entry.
            raw_topic = topic
            if raw_topic.lower() in ("general", "untitled", ""):
                raw_topic = entry_text
            if _append_thread(raw_topic, entry_text):
                threads_updated += 1

        processed_indices.add(idx)

    # Archive legacy '- [x]' lines (older sweep versions left them in place,
    # and the pad was excluded from memory indexing — dead, unearchived
    # history). After this run they live in Journal/YYYY-MM.md, searchable.
    for idx in legacy_done:
        legacy_text = re.sub(r"^\s*-\s*\[\s*[xX]\s*\]\s*", "", lines[idx]).strip()
        if legacy_text:
            _append_journal(f"archived from scribble pad — {legacy_text}")
            archived += 1

    if new_entries or legacy_done:
        keep = [
            line for i, line in enumerate(lines)
            if i not in processed_indices and i not in legacy_done
        ]
        with _SCRIBBLE_LOCK:
            _write_scribble("\n".join(keep))

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
        "archived": archived,
    }


def get_scheduler() -> Optional[BackgroundScheduler]:
    """Return the singleton BackgroundScheduler, starting it if needed.
    Returns None if APScheduler is not installed.

    Two jobs on the same instance:
    - "scribble_sweep": sweep the pad every sweep_interval seconds.
    - "task_runner": execute due tasks (same interval; per-tick cap applies).
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
        _scheduler.add_job(
            run_due_tasks,
            IntervalTrigger(seconds=int(interval)),
            id="task_runner",
            name="Task Runner",
            replace_existing=True,
        )
        _scheduler.start()
    return _scheduler


def shutdown_scheduler() -> None:
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        _scheduler.shutdown(wait=False)
        _scheduler = None
