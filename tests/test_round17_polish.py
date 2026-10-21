"""Round 17: polish fixes.

Covers:
- R17-P1: APP_VERSION sourced from package metadata, not drift-prone hardcodes.
- R17-P2: backfill_next_run() repairs legacy task rows — dedupes identical
  pending NULL-next_run rows, backfills next_run for valid crons, leaves
  one-shot (invalid cron) rows alone.
- Memory panel backend: /api/memory/files index listing and /api/memory/file
  vault-boundary-validated reader (traversal, non-.md, missing-file rejections).
- R17-P5: saving LLM settings kicks a due-tasks pass in the background.
- R17-P3: /api/runner/status mirrors the runner's own "LLM configured?" check.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import core.database as database
import core.scheduler as scheduler
import server

# Short alias the P5/P3 tests use.
core_scheduler = scheduler


# ── R17-P2: backfill_next_run ────────────────────────────────────────────


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    db = tmp_path / "agent.db"
    monkeypatch.setattr(database, "DB_PATH", db)
    database.init_db()
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    return conn


def _add_task(conn, title, cron, next_run=None, status="pending"):
    cur = conn.execute(
        "INSERT INTO tasks (title, cron_schedule, status, next_run, last_run) "
        "VALUES (?, ?, ?, ?, NULL)",
        (title, cron, status, next_run),
    )
    conn.commit()
    return cur.lastrowid


class TestBackfillNextRun:
    def test_dedupes_identical_legacy_pairs(self, tmp_db, monkeypatch):
        a = _add_task(tmp_db, "Water the plants", "0 2 * * *")
        b = _add_task(tmp_db, "Water the plants", "0 2 * * *")
        scheduler.backfill_next_run()
        rows = tmp_db.execute("SELECT id FROM tasks ORDER BY id").fetchall()
        ids = [r["id"] for r in rows]
        assert a in ids
        assert b not in ids  # duplicate removed
        assert len(ids) == 1

    def test_backfills_valid_cron(self, tmp_db):
        _add_task(tmp_db, "Brush teeth", "0 8 * * *")
        scheduler.backfill_next_run()
        row = tmp_db.execute("SELECT next_run FROM tasks").fetchone()
        assert row["next_run"] is not None
        assert row["next_run"].endswith("+00:00")  # UTC ISO stamp

    def test_leaves_invalid_cron_rows_alone(self, tmp_db):
        _add_task(tmp_db, "One-shot note", "not-a-cron")
        scheduler.backfill_next_run()
        row = tmp_db.execute("SELECT next_run FROM tasks").fetchone()
        assert row["next_run"] is None  # stays an honest one-shot

    def test_never_touches_done_rows_or_scheduled_rows(self, tmp_db):
        _add_task(tmp_db, "Done thing", "0 2 * * *", next_run=None, status="done")
        already = "2026-01-01T00:00:00+00:00"
        _add_task(tmp_db, "Scheduled thing", "0 5 * * *", next_run=already)
        result = scheduler.backfill_next_run()
        done = tmp_db.execute("SELECT next_run FROM tasks WHERE title='Done thing'").fetchone()
        assert done["next_run"] is None
        scheduled = tmp_db.execute(
            "SELECT next_run FROM tasks WHERE title='Scheduled thing'"
        ).fetchone()
        assert scheduled["next_run"] == already
        assert result["backfilled"] == 0

    def test_reports_counts(self, tmp_db):
        _add_task(tmp_db, "A", "0 2 * * *")
        _add_task(tmp_db, "A", "0 2 * * *")
        _add_task(tmp_db, "B", "0 3 * * *")
        result = scheduler.backfill_next_run()
        assert result == {"deduped": 1, "backfilled": 2}


# ── R17-P1: version sourcing ─────────────────────────────────────────────


class TestVersionSourcing:
    def test_app_version_is_a_populated_string(self):
        assert isinstance(server.APP_VERSION, str) and server.APP_VERSION

    def test_app_version_matches_installed_metadata_when_installed(self):
        from importlib.metadata import PackageNotFoundError, version
        try:
            installed = version("freehand-agent")
        except PackageNotFoundError:
            pytest.skip("freehand-agent not installed (bare test run); fallback in use")
        assert server.APP_VERSION == installed

    def test_fastapi_app_carries_that_version(self):
        assert server.app.version == server.APP_VERSION


# ── Memory panel backend ──────────────────────────────────────────────────


@pytest.fixture()
def memory_env(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    db = vault / "agent.db"
    monkeypatch.setattr(database, "DB_PATH", db)
    monkeypatch.setattr(server, "DB_PATH", db)
    monkeypatch.setattr(server, "VAULT_DIR", vault)
    monkeypatch.setattr(scheduler, "VAULT_DIR", vault)
    import core.memory as memory
    monkeypatch.setattr(memory, "VAULT_DIR", vault)
    monkeypatch.setattr(memory, "DB_PATH", db)
    monkeypatch.setattr(memory, "PROJECT_ROOT", vault.parent)
    database.init_db()
    (vault / "Journal").mkdir()
    (vault / "Journal" / "2026-10.md").write_text(
        "# Journal 2026-10\n\n- archived from scribble pad — dentist tomorrow\n",
        encoding="utf-8",
    )
    synced = memory.sync_vault_to_sqlite()
    return {"vault": vault, "synced": synced}


class TestMemoryFilesEndpoint:
    def test_lists_indexed_files(self, memory_env):
        files = asyncio.run(server.memory_files())
        paths = [f["path"] for f in files]
        # Stored rel paths use the OS separator on Windows.
        assert any(p.endswith("2026-10.md") and "Journal" in p for p in paths)
        assert all("title" in f and "updated_at" in f for f in files)


class TestMemoryFileEndpoint:
    def test_reads_an_indexed_vault_file(self, memory_env):
        out = asyncio.run(server.memory_file(path="vault/Journal/2026-10.md"))
        assert out["path"] == "vault/Journal/2026-10.md"
        assert "dentist tomorrow" in out["content"]

    def test_rejects_empty_path(self, memory_env):
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc:
            asyncio.run(server.memory_file(path=""))
        assert exc.value.status_code == 400

    def test_rejects_paths_escaping_the_vault(self, memory_env):
        from fastapi import HTTPException
        for sneaky in [
            "../freehand/static/index.html",
            "vault/../core/database.py",
            "freehand/static/index.html",  # inside project, outside vault
        ]:
            with pytest.raises(HTTPException) as exc:
                asyncio.run(server.memory_file(path=sneaky))
            assert exc.value.status_code == 400, sneaky

    def test_rejects_non_markdown(self, memory_env):
        from fastapi import HTTPException
        sneaky = memory_env["vault"] / "doc.db"
        sneaky.write_bytes(b"x")
        with pytest.raises(HTTPException) as exc:
            asyncio.run(server.memory_file(path="vault/doc.db"))
        assert exc.value.status_code == 400

    def test_reports_drift_as_404(self, memory_env):
        from fastapi import HTTPException
        (memory_env["vault"] / "Journal" / "2026-10.md").unlink()
        with pytest.raises(HTTPException) as exc:
            asyncio.run(server.memory_file(path="vault/Journal/2026-10.md"))
        assert exc.value.status_code == 404
        assert "sync" in str(exc.value.detail).lower()


# ── R17-P5/P3: LLM-save → immediate due-tasks pass; runner status ───────


class TestSaveSettingsTriggersRunner:
    def _patch_settings_path(self, tmp_path, monkeypatch) -> Path:
        settings_path = tmp_path / "settings.json"
        settings_path.write_text(json.dumps({"tier": "semi_autonomous"}))
        monkeypatch.setattr(server, "VAULT_SETTINGS_PATH", settings_path)
        return settings_path

    def test_saving_llm_key_triggers_a_due_tasks_pass(self, tmp_path, monkeypatch):
        self._patch_settings_path(tmp_path, monkeypatch)
        calls = []
        monkeypatch.setattr(
            core_scheduler, "run_due_tasks", lambda: calls.append(1) or {"executed": 0, "failed": 0}
        )
        result = asyncio.run(server.save_settings(
            {"llm": {"provider": "openai", "model": "gpt-4o-mini", "api_key": "sk-test"}}
        ))
        assert result["status"] == "saved"
        assert result["runner_triggered"] is True
        assert calls, "runner should have been invoked asynchronously"

    def test_saving_non_llm_settings_does_not_trigger_runner(self, tmp_path, monkeypatch):
        settings_path = self._patch_settings_path(tmp_path, monkeypatch)
        calls = []
        monkeypatch.setattr(
            core_scheduler, "run_due_tasks", lambda: calls.append(1) or {"executed": 0, "failed": 0}
        )
        result = asyncio.run(server.save_settings({"tier": "god_mode"}))
        assert result["status"] == "saved"
        assert result["runner_triggered"] is False
        assert not calls

    def test_saving_empty_llm_config_does_not_trigger(self, tmp_path, monkeypatch):
        self._patch_settings_path(tmp_path, monkeypatch)
        calls = []
        monkeypatch.setattr(
            core_scheduler, "run_due_tasks", lambda: calls.append(1) or {"executed": 0, "failed": 0}
        )
        result = asyncio.run(server.save_settings({"llm": {"api_key": "   "}}))
        assert result["runner_triggered"] is False
        assert not calls

    def test_saved_settings_persist_after_llm_save(self, tmp_path, monkeypatch):
        settings_path = self._patch_settings_path(tmp_path, monkeypatch)
        monkeypatch.setattr(
            core_scheduler, "run_due_tasks", lambda: {"executed": 0, "failed": 0}
        )
        asyncio.run(server.save_settings(
            {"llm": {"api_key": "sk-test"}}
        ))
        saved = json.loads(settings_path.read_text())
        assert saved["llm"]["api_key"] == "sk-test"


class TestRunnerStatusEndpoint:
    def _patch_agent_config(self, tmp_path, monkeypatch, llm: dict | None):
        import core.agent_config as agent_config
        settings_path = tmp_path / "settings.json"
        settings_path.write_text(
            json.dumps({"llm": llm} if llm is not None else {})
        )
        monkeypatch.setattr(agent_config, "SETTINGS_PATH", settings_path)

    def test_idle_when_no_llm_configured(self, tmp_path, monkeypatch):
        self._patch_agent_config(tmp_path, monkeypatch, None)
        status = asyncio.run(server.runner_status())
        assert status["llm_configured"] is False
        assert status["provider"] is None

    def test_active_when_api_key_present(self, tmp_path, monkeypatch):
        self._patch_agent_config(
            tmp_path, monkeypatch,
            {"provider": "openai", "model": "gpt-4o-mini", "api_key": "sk-x"},
        )
        status = asyncio.run(server.runner_status())
        assert status["llm_configured"] is True
        assert status["provider"] == "openai"

    def test_whitespace_key_counts_as_unconfigured(self, tmp_path, monkeypatch):
        """Mirrors run_due_tasks' own .strip() check — a blank key must not
        make the UI claim the runner is active."""
        self._patch_agent_config(
            tmp_path, monkeypatch,
            {"provider": "openai", "model": "gpt-4o-mini", "api_key": "  ", "base_url": ""},
        )
        status = asyncio.run(server.runner_status())
        assert status["llm_configured"] is False

    def test_matches_the_runners_own_gate(self, tmp_path, monkeypatch):
        """runner_status and run_due_tasks must agree on 'is there an LLM?'"""
        self._patch_agent_config(tmp_path, monkeypatch, None)
        monkeypatch.setattr(scheduler, "_TASK_RUNNER_WARNED", True)
        result = scheduler.run_due_tasks()
        status = asyncio.run(server.runner_status())
        assert result["skipped"] == "llm_not_configured"
        assert status["llm_configured"] is False
