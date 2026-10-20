"""Round 16: sweeper placement rework + task executor.

Covers:
- Cron validation (R16-C1): _parse_cron / _cron_next validate 5-field cron;
  garbage like '[*wip*]' no longer becomes a task schedule.
- Task executor (R16-C2): create_task computes next_run; run_due_tasks
  executes due tasks through run_agent with a per-tick cap, recomputes
  next_run for recurring tasks, marks one-shot tasks done, records
  last_run (with the error) on failures.
- LLM classification (R16-C3): _classify_via_llm is skipped without an
  api_key/base_url; the sweep falls through OmniRoute → LLM → heuristic.
- Placement (R16-C4): task entries are archived into a searchable
  Journal/YYYY-MM.md; the pad is compacted; legacy '- [x]' lines are
  archived once; topic threads get one file per topic.
- Gateway integration (R16-C5): /scribble now lands in sweepable
  checklist format; /task validates cron via create_task.
- POST /api/tasks rejects invalid cron with 400.
"""

import asyncio
import json
from datetime import datetime, timezone

import pytest


@pytest.fixture
def isolated_vault(tmp_path, monkeypatch):
    """Point scheduler + gateway at a fresh vault dir + isolated DB."""
    vault = tmp_path / "vault"
    vault.mkdir()
    db = vault / "agent.db"

    import core.database as database
    from core import gateway, scheduler as sched_mod

    monkeypatch.setattr(database, "DB_PATH", db)
    # core.memory binds DB_PATH via from-import — patch it too, or archived
    # history tests would query the real vault DB (same pattern as
    # test_round4_fixes.py).
    monkeypatch.setattr("core.memory.DB_PATH", db)
    # core.memory walks its own VAULT_DIR constant — point it at the tmp vault.
    # rel-path computation anchors on memory.PROJECT_ROOT, patch that too.
    import core.memory as memory_mod
    monkeypatch.setattr(memory_mod, "VAULT_DIR", vault)
    monkeypatch.setattr(memory_mod, "PROJECT_ROOT", vault.parent)
    monkeypatch.setattr(gateway, "VAULT_DIR", vault)
    monkeypatch.setattr(gateway, "SCRIBBLE_PATH", vault / "00_Scribble.md")
    monkeypatch.setattr(sched_mod, "VAULT_DIR", vault)
    monkeypatch.setattr(sched_mod, "SCRIBBLE_PATH", vault / "00_Scribble.md")
    monkeypatch.setattr(sched_mod, "THREADS_DIR", vault / "Threads")
    monkeypatch.setattr(sched_mod, "SWEEP_STATE_PATH", vault / ".sweep_state.json")
    database.init_db()
    yield vault


# ── R16-C1: cron validation ──────────────────────────────────────────

class TestCronValidation:
    def test_valid_exprs_pass(self):
        from core.scheduler import _parse_cron
        assert _parse_cron("* * * * *") is not None
        assert _parse_cron("0 2 * * *") is not None
        assert _parse_cron("*/5 9-17 * * 1-5") is not None
        assert _parse_cron("0 0 1,15 * *") is not None
        assert _parse_cron("30 8 1 */2 *") is not None

    def test_garbage_rejected(self):
        from core.scheduler import _parse_cron
        assert _parse_cron("wip*") is None
        assert _parse_cron("[wip*]") is None
        assert _parse_cron("0 2 * *") is None          # 4 fields
        assert _parse_cron("60 2 * * *") is None       # minute out of range
        assert _parse_cron("0 25 * * *") is None       # hour out of range
        assert _parse_cron("banana") is None
        assert _parse_cron("a b c d e") is None

    def test_dow_7_is_sunday(self):
        from core.scheduler import _parse_cron
        assert _parse_cron("0 2 * * 7") is not None
        fields = _parse_cron("0 2 * * 7")
        assert 0 in fields["dow"]

    def test_cron_next_basic(self):
        from core.scheduler import _cron_next
        after = datetime(2026, 10, 20, 10, 30, 0, tzinfo=timezone.utc)
        result = _cron_next("0 2 * * *", after=after)
        assert result == datetime(2026, 10, 21, 2, 0, 0, tzinfo=timezone.utc)
        result = _cron_next("*/15 * * * *", after=after)
        assert result.minute == 45 and result.hour == 10

    def test_cron_next_never_fires_returns_none(self):
        from core.scheduler import _cron_next
        assert _cron_next("0 0 30 2 *") is None

    def test_extract_cron_rejects_garbage(self):
        from core.scheduler import _extract_cron
        assert _extract_cron("Ship thing [*wip*]") == ""
        assert _extract_cron("Do it") == ""
        assert _extract_cron("Cron the plot [*/15 * * * *]") == "*/15 * * * *"


# ── R16-C2: task creation + executor ─────────────────────────────────

class TestTaskExecutor:
    @pytest.fixture(autouse=True)
    def _llm_configured(self, monkeypatch):
        """The task runner gates on get_llm_config — patch it at the point
        core.scheduler imports it (not inside core.agent_config)."""
        import core.scheduler as scheduler
        monkeypatch.setattr(scheduler, "get_llm_config",
                            lambda: {"model": "gpt-4o-mini", "api_key": "k", "base_url": ""})
        monkeypatch.setattr(scheduler, "_TASK_RUNNER_WARNED", False)
    def test_create_task_sets_next_run(self, isolated_vault):
        import sqlite3
        import core.database as database
        from core.scheduler import create_task
        result = create_task("Water the plants", "0 2 * * *")
        assert result["recurring"] is True
        assert result["next_run"] is not None
        conn = sqlite3.connect(str(database.DB_PATH))
        row = conn.execute(
            "SELECT title, cron_schedule, status, next_run FROM tasks WHERE id = ?",
            (result["id"],),
        ).fetchone()
        conn.close()
        assert row == ("Water the plants", "0 2 * * *", "pending", result["next_run"])

    def test_create_task_invalid_cron_is_one_shot(self, isolated_vault):
        from core.scheduler import create_task
        result = create_task("File taxes", "banana cron")
        assert result["recurring"] is False
        assert result["next_run"] is None

    def test_run_due_tasks_runs_one_shot_once(self, isolated_vault, monkeypatch):
        from core.scheduler import create_task, run_due_tasks
        import core.agent as agent_mod

        monkeypatch.setattr(agent_mod, "run_agent", None)  # replaced below
        calls = []

        async def fake_run_agent(command, max_turns=5, source="", caller_id=""):
            calls.append(command)
            return {"text": "done", "tools_used": [], "error": None}

        monkeypatch.setattr("core.agent.run_agent", fake_run_agent)

        created = create_task("One-off reminder", "banana")  # one-shot
        assert created["next_run"] is None

        result = run_due_tasks()
        assert result["executed"] == 1
        assert result["failed"] == 0
        assert calls == ["One-off reminder"]

        # Marked done: a second pass must not re-execute
        result = run_due_tasks()
        assert result["executed"] == 0

    def test_run_due_tasks_bumps_recurring_next_run(self, isolated_vault, monkeypatch):
        from core.scheduler import create_task, run_due_tasks, _iso_utc
        from datetime import datetime, timezone

        async def fake_run_agent(command, max_turns=5, source="", caller_id=""):
            return {"text": "done", "tools_used": [], "error": None}

        monkeypatch.setattr("core.agent.run_agent", fake_run_agent)

        now = datetime(2026, 10, 20, 1, 59, 0, tzinfo=timezone.utc)
        result = create_task("Daily digest", "0 2 * * *")
        # Force due now
        import sqlite3
        import core.database as database
        conn = sqlite3.connect(str(database.DB_PATH))
        conn.execute("UPDATE tasks SET next_run = ? WHERE id = ?",
                     (_iso_utc(now), result["id"]))
        conn.commit()
        conn.close()

        outcome = run_due_tasks(now=now)
        assert outcome["executed"] == 1

        conn = sqlite3.connect(str(database.DB_PATH))
        row = conn.execute("SELECT status, next_run FROM tasks WHERE id = ?",
                           (result["id"],)).fetchone()
        conn.close()
        assert row[0] == "pending"
        # Next run is tomorrow 02:00 UTC
        nxt = datetime.fromisoformat(row[1])
        assert nxt == datetime(2026, 10, 20, 2, 0, 0, tzinfo=timezone.utc)
        # i.e. the very next 02:00 UTC after now=01:59 on the same day

    def test_run_due_tasks_records_failure(self, isolated_vault, monkeypatch):
        from core.scheduler import create_task, run_due_tasks

        async def failing_agent(command, max_turns=5, source="", caller_id=""):
            return {"text": "", "tools_used": [], "error": "LLM API error 500"}

        monkeypatch.setattr("core.agent.run_agent", failing_agent)
        created = create_task("Flaky job", "banana")  # one-shot
        outcome = run_due_tasks()
        assert outcome["failed"] == 1
        # One-shot failure stays pending for retry
        import sqlite3
        import core.database as database
        conn = sqlite3.connect(str(database.DB_PATH))
        row = conn.execute("SELECT status, last_run FROM tasks WHERE id = ?",
                           (created["id"],)).fetchone()
        conn.close()
        assert row[0] == "pending"
        assert "error" in row[1]

    def test_run_due_tasks_per_tick_cap(self, isolated_vault, monkeypatch):
        from core.scheduler import run_due_tasks, create_task

        async def fake_agent(command, max_turns=5, source="", caller_id=""):
            return {"text": "ok", "tools_used": [], "error": None}

        monkeypatch.setattr("core.agent.run_agent", fake_agent)
        (isolated_vault / "settings.json").write_text('{"task_max_per_tick": 2}')

        for i in range(4):
            create_task(f"Task {i}", "banana")
        outcome = run_due_tasks()
        assert outcome["executed"] == 2

    def test_run_due_tasks_skips_without_llm(self, isolated_vault, monkeypatch):
        import core.scheduler as scheduler
        from core.scheduler import create_task, run_due_tasks
        created = create_task("Never runs", "0 2 * * *")
        monkeypatch.setattr(scheduler, "get_llm_config",
                            lambda: {"model": "gpt-4o-mini", "api_key": "", "base_url": ""})
        outcome = run_due_tasks()
        assert outcome["skipped"] == "llm_not_configured"
        import sqlite3
        import core.database as database
        conn = sqlite3.connect(str(database.DB_PATH))
        row = conn.execute("SELECT status FROM tasks WHERE id = ?", (created["id"],)).fetchone()
        conn.close()
        assert row[0] == "pending"  # stays due, not silently consumed


# ── R16-C3: classification chain ─────────────────────────────────────

class TestClassification:
    @pytest.fixture(autouse=True)
    def _llm_configured(self, monkeypatch):
        import core.scheduler as scheduler
        monkeypatch.setattr(scheduler, "get_llm_config",
                            lambda: {"model": "gpt-4o-mini", "api_key": "k", "base_url": ""})

    def test_llm_classification_skipped_when_not_configured(self, isolated_vault, monkeypatch):
        import core.scheduler as scheduler
        monkeypatch.setattr(scheduler, "get_llm_config",
                            lambda: {"model": "gpt-4o-mini", "api_key": "", "base_url": ""})
        from core.scheduler import _classify_via_llm
        assert _classify_via_llm("buy milk") is None

    def test_llm_classification_parses_json(self, isolated_vault, monkeypatch):
        from core import scheduler

        async def fake_call_llm(messages, tools=None, config=None):
            return {"role": "assistant", "content": json.dumps({
                "classification": "note", "topic": "gardening plans", "cron": ""})}

        monkeypatch.setattr("core.agent.call_llm", fake_call_llm)
        result = scheduler._classify_via_llm("thoughts about the garden")
        assert result == {"classification": "note", "topic": "gardening plans", "cron": ""}

    def test_sweep_uses_classify_chain(self, isolated_vault, monkeypatch):
        """OmniRoute none (unset) → LLM none (not configured) → heuristic."""
        from core.scheduler import process_scribble, SCRIBBLE_PATH, THREADS_DIR
        SCRIBBLE_PATH.write_text("# pad\n\n- [ ] remember the bins on Tuesday\n", encoding="utf-8")
        result = process_scribble()
        assert result["processed"] == 1
        assert result["tasks_created"] == 1  # heuristic: keyword 'remember'
        assert THREADS_DIR.exists() is False or not any(THREADS_DIR.iterdir())


# ── R16-C4: placement + archiving ────────────────────────────────────

class TestPlacement:
    def test_task_entry_archived_to_journal_and_pad_compacted(self, isolated_vault):
        from core.scheduler import process_scribble, SCRIBBLE_PATH
        from datetime import datetime, timezone
        SCRIBBLE_PATH.write_text("# pad\n\n- [ ] reminder call mom about tickets\n", encoding="utf-8")
        result = process_scribble()
        assert result["tasks_created"] == 1
        # Pad is compacted: the entry is gone
        content = SCRIBBLE_PATH.read_text(encoding="utf-8")
        assert "call mom" not in content
        # Journal holds the audit trail, searchable by memory sync
        journal = isolated_vault / "Journal"
        assert journal.exists()
        ym = datetime.now(timezone.utc).strftime("%Y-%m")
        journal_text = (journal / f"{ym}.md").read_text(encoding="utf-8")
        assert "call mom" in journal_text

    def test_note_entry_lands_in_topic_thread(self, isolated_vault, monkeypatch):
        from core import scheduler
        from core.scheduler import process_scribble, SCRIBBLE_PATH, THREADS_DIR

        monkeypatch.setattr(scheduler, "_call_omniroute", lambda text: None)
        monkeypatch.setattr(scheduler, "_classify_via_llm", lambda text: None)
        monkeypatch.setattr(scheduler, "_classify_heuristic",
                            lambda text: {"classification": "thread", "topic": "garden"})

        SCRIBBLE_PATH.write_text("# pad\n\n- [ ] the tomatoes need staking\n", encoding="utf-8")
        result = process_scribble()
        assert result["threads_updated"] == 1
        thread_files = list(THREADS_DIR.glob("*.md"))
        assert len(thread_files) == 1
        assert "garden" in thread_files[0].name
        assert "tomatoes" in thread_files[0].read_text(encoding="utf-8")
        # Compacted pad
        assert "tomatoes" not in SCRIBBLE_PATH.read_text(encoding="utf-8")

    def test_legacy_done_lines_archived_once(self, isolated_vault, monkeypatch):
        from core import scheduler
        monkeypatch.setattr(scheduler, "_call_omniroute", lambda text: None)
        monkeypatch.setattr(scheduler, "_classify_via_llm", lambda text: None)
        from core.scheduler import process_scribble, SCRIBBLE_PATH
        pad = "# pad\n\n- [x] an old processed line\n\n- [ ] remember the bins\n"
        SCRIBBLE_PATH.write_text(pad, encoding="utf-8")
        process_scribble()
        content = SCRIBBLE_PATH.read_text(encoding="utf-8")
        assert "old processed line" not in content
        assert "remember the bins" not in content
        # Second sweep must not duplicate the archive entry
        journal_files = list((isolated_vault / "Journal").glob("*.md"))
        text = "".join(f.read_text(encoding="utf-8") for f in journal_files)
        assert text.count("old processed line") == 1
        process_scribble()
        text2 = "".join(f.read_text(encoding="utf-8") for f in journal_files)
        assert text2.count("old processed line") == 1

    def test_archived_history_is_fts5_indexed(self, isolated_vault, monkeypatch):
        from core import scheduler as scheduler_mod
        from core.scheduler import process_scribble, SCRIBBLE_PATH
        from core.memory import sync_vault_to_sqlite, search_memory

        monkeypatch.setattr(scheduler_mod, "_call_omniroute", lambda text: None)
        monkeypatch.setattr(scheduler_mod, "_classify_via_llm", lambda text: None)
        SCRIBBLE_PATH.write_text("# pad\n\n- [ ] reminder call mom about tickets\n", encoding="utf-8")
        process_scribble()
        sync_vault_to_sqlite()
        results = search_memory("call mom")
        assert results, "Journal archive should be searchable memory"


# ── R16-C5: gateway commands ─────────────────────────────────────────

class TestGatewayCommands:
    def test_gateway_scribble_uses_checklist_format(self, isolated_vault):
        from core.gateway import _handle_scribble_command
        from core.scheduler import SCRIBBLE_PATH
        reply = asyncio.run(_handle_scribble_command(
            "/scribble check the flight status", "telegram", "123"))
        assert "Added to Scribble" in reply
        content = SCRIBBLE_PATH.read_text(encoding="utf-8")
        assert content.strip().splitlines()[-1].startswith("- [ ] [")
        assert "check the flight status" in content

    def test_gateway_scribble_entry_is_sweepable_as_task(self, isolated_vault, monkeypatch):
        """Regression for the black-hole bug: a remotely captured note that
        mentions 'task' must actually reach the tasks table via the sweep."""
        from core.scheduler import append_scribble, process_scribble, SCRIBBLE_PATH
        append_scribble("task schedule the dentist appointment")
        result = process_scribble()
        assert result["tasks_created"] == 1
        assert SCRIBBLE_PATH.exists()

    def test_gateway_task_validates_cron(self, isolated_vault):
        from core.gateway import _route_to_agent
        reply = asyncio.run(_route_to_agent(
            "/task file taxes [banana cron]", "telegram", "123"))
        assert "one-shot" in reply.lower()

    def test_gateway_task_recurring_reports_next_run(self, isolated_vault):
        from core.gateway import _route_to_agent
        reply = asyncio.run(_route_to_agent(
            "/task daily digest [0 2 * * *]", "telegram", "123"))
        assert "Task created" in reply
        assert "next:" in reply


# ── POST /api/tasks: invalid cron → 400 ──────────────────────────────

class TestApiTasksValidation:

    def test_create_task_endpoint_rejects_invalid_cron(self, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient
        import server
        import core.database as database

        vault = tmp_path / "vault"
        vault.mkdir()
        db = vault / "agent.db"
        monkeypatch.setattr(database, "DB_PATH", db)
        monkeypatch.setattr("server.DB_PATH", db)
        monkeypatch.setattr(server, "SCRIBBLE_PATH", vault / "00_Scribble.md")
        database.init_db()

        """Task creation is not asyncio-heavy; run in a plain loop."""

        monkeypatch.setattr("server.get_or_create_api_key", lambda: "testkey123")

        client = TestClient(server.app)

        r = client.post("/api/tasks", headers={"X-API-Key": "testkey123"},
                        json={"title": "Water", "cron_schedule": "not-a-cron"})
        assert r.status_code == 400
        assert "cron" in r.json()["detail"].lower()

        r_ok = client.post("/api/tasks", headers={"X-API-Key": "testkey123"},
                           json={"title": "Water", "cron_schedule": "0 2 * * *"})
        assert r_ok.status_code == 200
        body = r_ok.json()
        assert body["recurring"] is True
        assert body["next_run"] is not None
