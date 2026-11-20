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
DUE_PREFIX = "It's time for a scheduled task the person set up earlier: "
MAX_FAILURES = 3  # failures in a row before a task pauses itself
JOB_STEPS = 5  # most model steps a "think" job may take per run
# How often scheduled jobs may use AI each month (Tracy's choice, Nov 2026:
# counted in uses, which works the same for every model, priced or not).
LIMIT_PER_JOB = 100
LIMIT_TOTAL = 300


def limits() -> dict:
    """{"per_job": n, "total": n}, from settings "job_limits" if set."""
    saved = _load_settings().get("job_limits") or {}
    out = {"per_job": LIMIT_PER_JOB, "total": LIMIT_TOTAL}
    for key in out:
        try:
            value = int(saved.get(key))
        except (TypeError, ValueError, AttributeError):
            continue
        if 0 <= value <= 100_000:
            out[key] = value
    return out
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
    from core import people
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(people.carry(asyncio.run), coro).result()  # as the same person


def _load_settings() -> dict:
    settings_path = VAULT_DIR / "settings.json"
    if settings_path.exists():
        try:
            return json.loads(settings_path.read_text())
        except Exception:
            pass
    return {}


def local_tz():
    """The person's time zone, set from their browser, so "every day at 9"
    means 9 where they are. The owner's is settings "timezone"; a member's
    is on them in "people" (core/people.py), and until their browser has
    sent one, the owner's is theirs too. UTC until it's known."""
    settings = _load_settings()
    from core import people
    name = people.timezone_of(people.current(), settings) or str(settings.get("timezone") or "").strip()
    if name:
        try:
            from zoneinfo import ZoneInfo
            return ZoneInfo(name)
        except Exception:
            pass
    return timezone.utc


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


def _llm_ready() -> bool:
    """Is there a model to use: the main one (get_llm_config, so tests can
    patch it here) or any backup (Nov 2026)?"""
    from core import chatgpt_plan
    from core.agent_config import get_llm_backups, llm_is_configured
    return (llm_is_configured(get_llm_config()) or bool(get_llm_backups())
            or chatgpt_plan.anyone_signed_in())  # someone's own ChatGPT plan


def _classify_via_llm(text: str) -> Optional[dict]:
    """Classify a scribble entry using the LLM configured in settings.llm.

    Returns {"classification": "task"|"note", "topic": str, "cron": str},
    or None when no LLM is configured, the call fails, or the reply is
    unparseable — the caller then falls back to the keyword heuristic.
    """
    try:
        if not _llm_ready():
            return None
        from core.agent import call_llm
        # No config passed: the main model, then the backups (Nov 2026).
        resp = _run_coro(call_llm([
            {"role": "system", "content": _CLASSIFY_SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ]))
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


def _cron_next(expr: str, after: Optional[datetime] = None, tz=None) -> Optional[datetime]:
    """Next UTC datetime matching a 5-field cron expression, or None.

    Cron is read in ``tz`` (the person's time zone, see local_tz), UTC if
    not given; the answer is always in UTC. Brute-force
    minute stepping capped at 366 days; expressions that never fire inside a
    year (e.g. '0 0 30 2 *') return None.
    """
    fields = _parse_cron(expr)
    if fields is None:
        return None
    start = after or datetime.now(timezone.utc)
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    dt = start.astimezone(tz or timezone.utc)
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
                return dt.astimezone(timezone.utc)
        dt += timedelta(minutes=1)
    return None


def _extract_cron(entry: str) -> str:
    """Extract a bracketed cron-like schedule if (and only if) it is a
    valid 5-field cron expression. Anything else — e.g. the note '[wip*]' —
    returns '' (no schedule) instead of storing
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


def _workspace(person_id: Optional[str] = None) -> Path:
    """The person's own folder in the vault (core/people.py): the vault
    itself for the owner, people/<id>/ for anyone else."""
    from core import people
    return VAULT_DIR.joinpath(*people.workspace_parts(person_id))


def _is_owner(person_id: Optional[str] = None) -> bool:
    from core import people
    return (person_id or people.current()) == people.OWNER_ID


def _scribble_path(person_id: Optional[str] = None) -> Path:
    return SCRIBBLE_PATH if _is_owner(person_id) else _workspace(person_id) / "00_Scribble.md"


def _sweep_state_path(person_id: Optional[str] = None) -> Path:
    return SWEEP_STATE_PATH if _is_owner(person_id) else _workspace(person_id) / ".sweep_state.json"


def _threads_dir(person_id: Optional[str] = None) -> Path:
    return THREADS_DIR if _is_owner(person_id) else _workspace(person_id) / "Threads"


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
    threads_dir = _threads_dir()
    candidate = (threads_dir / (slug + ".md")).resolve()
    threads_root = threads_dir.resolve()
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
    """The current person's own scribble pad."""
    path = _scribble_path()
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def _write_scribble(content: str) -> None:
    path = _scribble_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


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
    path = _sweep_state_path()
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            pass
    return {"last_sweep": "", "processed_lines": []}


def _save_sweep_state(state: dict) -> None:
    path = _sweep_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state), encoding="utf-8")


def _append_journal(entry: str) -> None:
    """Append a timestamped line to vault/Journal/YYYY-MM.md.

    Journal files are plain .md inside the vault, so sync_vault_to_sqlite
    indexes them — archived history is searchable memory, not dead weight.
    """
    now = datetime.now(timezone.utc)
    journal_dir = _workspace() / "Journal"
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
    threads_dir = _threads_dir()
    if not threads_dir.exists():
        threads_dir.mkdir(parents=True, exist_ok=True)
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


# Kinds of job (docs/design/scheduled-jobs-and-team-members.md): "do" runs
# in code with no model at all; "think" asks the model each time.
# "check" runs a cheap check in code, and only tells (or wakes the model)
# when it finds something (core/checks.py).
KINDS = ("do", "check", "think")


def _ensure_deliver_column(conn: sqlite3.Connection) -> None:
    """Columns added in Nov 2026: where the result goes (core/notices.py),
    failures in a row, the kind of job and its action, and paused jobs."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
    if "deliver_to" not in cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN deliver_to TEXT")
    if "fail_count" not in cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN fail_count INTEGER NOT NULL DEFAULT 0")
    if "kind" not in cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN kind TEXT NOT NULL DEFAULT 'think'")
    if "action" not in cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN action TEXT")
    # What a job used this month: AI runs and tokens (build step 4).
    if "usage_month" not in cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN usage_month TEXT")
        conn.execute("ALTER TABLE tasks ADD COLUMN month_runs INTEGER NOT NULL DEFAULT 0")
        conn.execute("ALTER TABLE tasks ADD COLUMN month_tokens INTEGER NOT NULL DEFAULT 0")
    if "limit_told" not in cols:  # the month the person was told a limit was reached
        conn.execute("ALTER TABLE tasks ADD COLUMN limit_told TEXT")
    if "team_member" not in cols:  # the team member who does the job (core/team.py)
        conn.execute("ALTER TABLE tasks ADD COLUMN team_member TEXT")
    conn.commit()


def _month(now: datetime) -> str:
    return now.astimezone(local_tz()).strftime("%Y-%m")


def ai_allowed(conn: sqlite3.Connection, row, now: datetime) -> Optional[str]:
    """None if this job may use AI now, or the reason it may not (a limit
    on uses this month was reached). A new month starts again."""
    month, lim = _month(now), limits()
    used = int(row["month_runs"] or 0) if row["usage_month"] == month else 0
    if used >= lim["per_job"]:
        return f"has used AI {lim['per_job']} times this month, which is its limit"
    # Each person's jobs together: one person's jobs can't use up everyone's.
    from core import ai_use, people
    person = row["person"] if "person" in row.keys() and row["person"] else people.OWNER_ID
    total = conn.execute("SELECT COALESCE(SUM(month_runs), 0) FROM tasks WHERE usage_month = ? AND person = ?",
                         (month, person)).fetchone()[0]
    if total >= lim["total"]:
        return f"can't use AI: all your scheduled jobs together have used it {lim['total']} times this month, which is the limit"
    if ai_use.refusal(person, now):
        return (f"can't use AI: you've used FreeHand's AI {ai_use.limit(person)} times this month, "
                f"which is the limit {people.list_people()[0]['name']} set")
    return None


def _next_month_name(now: datetime) -> str:
    local = now.astimezone(local_tz())
    first = (local.replace(day=28) + timedelta(days=4)).replace(day=1)
    return f"1 {first:%B}"


def _add_usage(conn: sqlite3.Connection, task_id: int, usage: Optional[dict], now: datetime) -> None:
    """Count one AI run and its tokens against this month (a new month
    starts again from nought)."""
    if not usage:
        return
    tokens = int(usage.get("prompt_tokens") or 0) + int(usage.get("completion_tokens") or 0)
    month = _month(now)
    row = conn.execute("SELECT usage_month FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        return
    if row[0] != month:
        conn.execute("UPDATE tasks SET usage_month = ?, month_runs = 1, month_tokens = ? WHERE id = ?",
                     (month, tokens, task_id))
    else:
        conn.execute("UPDATE tasks SET month_runs = month_runs + 1, month_tokens = month_tokens + ? WHERE id = ?",
                     (tokens, task_id))


def create_task(title: str, cron_schedule: str, deliver_to: Optional[dict] = None,
                status: str = "pending", kind: str = "think", action: Optional[dict] = None,
                run_at: Optional[datetime] = None, team_member: Optional[str] = None) -> dict:
    """Insert a task row and compute its first scheduled run.

    A valid 5-field cron makes the task recurring (next_run set; the runner
    re-computes after each run). An empty or invalid cron makes it one-shot
    (next_run stays NULL; the runner runs it once, then marks it done).
    Until Nov 2026 an empty cron became DEFAULT_CRON, so a task with no time
    ran every night at 2am, forever.

    deliver_to says where the result goes besides the web page, for example
    {"channel": "telegram", "to": "<chat id>"}. status "todo" keeps a to-do
    the runner never runs (a scribbled task with no time).

    kind "do" with an action (see run_action) runs in code, with no model;
    "think" asks the model. run_at makes it a one-off at that moment.
    Never raises in normal operation; DB failures surface to the caller.
    """
    title = (title or "").strip()
    cron = (cron_schedule or "").strip()
    if run_at is not None:
        cron = ""
        if run_at.tzinfo is None:
            run_at = run_at.replace(tzinfo=local_tz())
        next_run = run_at.astimezone(timezone.utc)
    else:
        next_run = _cron_next(cron, tz=local_tz()) if cron else None
    status = status if status in ("pending", "todo") else "pending"
    kind = kind if kind in KINDS else "think"
    from core import people
    conn = _tasks_conn()
    try:
        people.ensure_person_column(conn, "tasks")
        _ensure_deliver_column(conn)
        cursor = conn.execute(
            "INSERT INTO tasks (title, cron_schedule, status, next_run, last_run, person, deliver_to, kind, action, team_member) "
            "VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?)",
            (title, cron, status, _iso_utc(next_run) if next_run else None, people.current(),
             json.dumps(deliver_to) if deliver_to else None, kind,
             json.dumps(action) if action else None, team_member if kind == "think" else None),
        )
        conn.commit()
        task_id = cursor.lastrowid
        return {
            "id": task_id,
            "title": title,
            "cron_schedule": cron,
            "status": status,
            "next_run": _iso_utc(next_run) if next_run else None,
            "recurring": bool(cron) and next_run is not None,
            "kind": kind,
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
        tz = local_tz()
        for row in remaining:
            nxt = _cron_next(row["cron_schedule"], tz=tz)
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


def run_action(action, person: str = "owner") -> str:
    """A "do" job's work, in code. Returns the message to send.

    {"type": "remind", "text": "..."}  the reminder, word for word.
    {"type": "todos"}                  the person's to-do list.
    """
    if isinstance(action, str):
        try:
            action = json.loads(action)
        except ValueError:
            action = {}
    action = action if isinstance(action, dict) else {}
    kind = action.get("type")
    if kind == "remind":
        text = str(action.get("text") or "").strip()
        return text if text.lower().startswith("reminder") else "Reminder: " + text
    if kind == "todos":
        conn = _tasks_conn()
        try:
            rows = conn.execute(
                "SELECT title FROM tasks WHERE status = 'todo' AND person = ? ORDER BY id",
                (person,),
            ).fetchall()
        finally:
            conn.close()
        if not rows:
            return "Your to-do list is empty."
        return "Your to-do list:\n" + "\n".join("- " + r["title"] for r in rows)
    raise ValueError(f"unknown action: {kind!r}")


def _member_for(row) -> Optional[dict]:
    if not row["team_member"]:
        return None
    from core import team
    return team.get(row["team_member"])


def _due_prompt(title: str) -> str:
    """What the agent is told when a task is due. Without this, "Remind me
    to phone the dentist" got "Sure, I'll remind you" instead of a reminder."""
    return (
        DUE_PREFIX + f"\"{title}\". "
        "Do it now. If it's a reminder, write the reminder to them in a sentence "
        'or two, for example "Reminder: phone the dentist." Your reply is sent '
        "to them as it is."
    )


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
    from core import notices, people

    # "do" jobs need no model, so they run even when none is set up.
    llm_ok = _llm_ready()
    if not llm_ok and not _TASK_RUNNER_WARNED:
        print("[WARN] No LLM configured (settings.llm): only free scheduled jobs "
              "(reminders and lists) will run. Set an API key in the web UI for the rest.")
        _TASK_RUNNER_WARNED = True

    cap = int(_load_settings().get("task_max_per_tick", 3))
    cap = max(1, cap)
    now = now or datetime.now(timezone.utc)
    now_iso = _iso_utc(now)
    conn = _tasks_conn()
    executed = 0
    failed = 0
    before = None
    try:
        people.ensure_person_column(conn, "tasks")
        _ensure_deliver_column(conn)
        rows = conn.execute(
            "SELECT id, title, cron_schedule, next_run, person, deliver_to, fail_count, kind, action, "
            "usage_month, month_runs, limit_told, team_member FROM tasks "
            "WHERE status = 'pending' AND (next_run IS NULL OR next_run <= ?) "
            + ("" if llm_ok else "AND kind IN ('do', 'check') ")
            + "ORDER BY id LIMIT ?",
            (now_iso, cap),
        ).fetchall()
        before = people.set_current(people.current())  # put back when done
        for row in rows:
            # A job runs as the person who set it up, so it sees their
            # things, not the owner's. Someone who's no longer on this
            # FreeHand has their jobs stop (paused after a few tries).
            person = row["person"] or people.OWNER_ID
            people.set_current(person)
            if people.get(person) is None:
                result = {"text": "", "error": "person gone",
                          "plain": "the person who set it up is no longer on this FreeHand"}
            elif row["kind"] == "do":
                try:
                    result = {"text": run_action(row["action"], row["person"]), "error": None}
                except Exception as e:
                    result = {"text": "", "error": str(e)}
            elif row["kind"] == "check":
                from core import checks
                try:
                    action = json.loads(row["action"] or "{}")
                    # Over its AI limit, a check still checks and tells, free.
                    may_think = llm_ok and ai_allowed(conn, row, now) is None
                    text, kept = _run_coro(checks.run(action, llm_ok=may_think))
                    _add_usage(conn, row["id"], kept.pop("last_usage", None), now)
                    # What the check saw this time, so next time only new things count.
                    conn.execute("UPDATE tasks SET action = ? WHERE id = ?", (json.dumps(kept), row["id"]))
                    result = {"text": text, "error": None}
                except checks.CheckError as e:
                    result = {"text": "", "error": str(e), "plain": str(e)}
                except Exception as e:
                    result = {"text": "", "error": str(e)}
            elif ai_allowed(conn, row, now):
                # Over a limit: don't run. Say so once this month, and carry
                # on next month (or when the limit is raised).
                result = {"text": "", "error": None, "held": True}
                if row["limit_told"] != _month(now):
                    from core import people as _people
                    change = ("To change the limit, go to Scheduled jobs, Limits on AI use."
                              if person == _people.OWNER_ID else
                              f"Ask {_people.list_people()[0]['name']} if you need more.")
                    result["text"] = (f"Your scheduled job \"{row['title']}\" {ai_allowed(conn, row, now)}, "
                                      f"so it won't run again until {_next_month_name(now)}. {change}")
                    conn.execute("UPDATE tasks SET limit_told = ? WHERE id = ?", (_month(now), row["id"]))
            elif row["team_member"] and _member_for(row) is None:
                result = {"text": "", "error": "team member gone",
                          "plain": "the team member who did this job has been deleted, or isn't shared any more"}
            elif row["team_member"] and _member_for(row).get("paused"):
                result = {"text": "", "error": None, "held": True}  # resumes with them
            else:
                # Short instructions, no chat history, only the tools it needs,
                # and at most JOB_STEPS steps (core/agent.py, job=True). As a
                # team member: its own setup and limits (core/team.py).
                as_member = {"member": _member_for(row)} if row["team_member"] else {}
                result = _run_coro(run_agent(_due_prompt(row["title"]), max_turns=JOB_STEPS,
                                             source="cli", job=True, person=person, **as_member))
                _add_usage(conn, row["id"], result.get("usage"), now)
            error = result.get("error")
            reply = (result.get("text") or "").strip()
            fails = (row["fail_count"] or 0) + 1 if error else 0
            if error:
                # A failed one-off task is due again on the next tick, so only
                # speak up when it's paused, not every 30 seconds.
                reply = ""
                if fails >= MAX_FAILURES:
                    why = f" ({result['plain'].rstrip('.')})" if result.get("plain") else ""
                    reply = (f"I couldn't do your scheduled task \"{row['title']}\"{why}. "
                             f"I tried {fails} times, so I've stopped trying. To try again, "
                             "add it again under Scheduled jobs.")
            if reply:
                # The notice is written on its own connection: anything this
                # one has written must be saved first, or that write waits on
                # a locked database and the message is lost.
                conn.commit()
                _run_coro(notices.deliver(reply, title=row["title"],
                                          target=row["deliver_to"], person=row["person"]))
            stamp = _iso_utc(datetime.now(timezone.utc))
            err_note = (stamp + " error: " + str(error)[:120]) if error else stamp
            next_iso = None
            if row["next_run"]:
                # In the time zone of the person whose job it is.
                nxt = _cron_next(row["cron_schedule"], after=now, tz=local_tz()) if row["cron_schedule"] else None
                next_iso = _iso_utc(nxt) if nxt else None
            if result.get("held"):
                # A one-off waits; a repeating job skips to its next time.
                conn.execute("UPDATE tasks SET next_run = ? WHERE id = ?",
                             (next_iso if row["cron_schedule"] else row["next_run"], row["id"]))
            elif error:
                failed += 1
                conn.execute(
                    "UPDATE tasks SET last_run = ?, next_run = ?, fail_count = ?, status = ? WHERE id = ?",
                    (err_note, next_iso, fails, "paused" if fails >= MAX_FAILURES else "pending", row["id"]),
                )
            else:
                executed += 1
                new_status = "pending" if next_iso else "done"
                conn.execute(
                    "UPDATE tasks SET last_run = ?, next_run = ?, status = ?, fail_count = 0 WHERE id = ?",
                    (stamp, next_iso, new_status, row["id"]),
                )
        conn.commit()
    finally:
        if before is not None:
            people.reset(before)
        conn.close()
    return {"executed": executed, "failed": failed,
            "skipped": None if llm_ok else "llm_not_configured"}


# ──────────────────────────────────────────────────────────────────────
# The sweep
# ──────────────────────────────────────────────────────────────────────

def sweep_everyones_pads() -> dict:
    """The regular sweep: each person's pad, swept as that person, so
    their entries become their own tasks, Threads and Journal."""
    from core import people
    totals: dict = {}
    for person in people.list_people():
        pid = person["id"]
        if pid != people.OWNER_ID and not _scribble_path(pid).exists():
            continue
        token = people.set_current(pid)
        try:
            result = process_scribble()
        except Exception:
            continue  # one person's pad never stops the others' sweep
        finally:
            people.reset(token)
        for key, value in result.items():
            totals[key] = totals.get(key, 0) + value
    return totals


def process_scribble() -> dict:
    """The current person's pad: read new raw entries from 00_Scribble.md, classify them (OmniRoute →
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
            # No time given: a to-do to keep, not a job to run (it used to
            # become a job that ran every night at 2am).
            created = create_task(title, cron, status="pending" if cron else "todo")
            tasks_created += 1
            if not cron:
                sched_desc = "to-do, no time given, so FreeHand won't run it"
            elif created["recurring"]:
                sched_desc = f"cron {created['cron_schedule']}"
            else:
                sched_desc = "one-shot (cron could not be parsed)"
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
            sweep_everyones_pads,
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
