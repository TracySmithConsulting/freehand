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
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import core.database as database
import core.scheduler as scheduler
import server

try:
    from fastapi.testclient import TestClient
except ImportError:  # pragma: no cover — dev extra guarantees it, be safe
    TestClient = None

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

    def test_hides_stale_rows_pointing_at_missing_paths(self, memory_env):
        """Legacy garbage rows (e.g. fake '/agent/command' audit paths) must
        not appear in the file browser — they 404 noisily on Open."""
        import sqlite3 as _s
        conn = _s.connect(database.DB_PATH)
        conn.execute(
            "INSERT INTO memories (path, title, content, updated_at) "
            "VALUES ('/agent/command', 'Command (dead): test', 'x', 'now')"
        )
        conn.commit(); conn.close()
        files = asyncio.run(server.memory_files())
        assert all("/agent/command" not in f["path"] for f in files)


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


# ── JS replay regression: real index.html script renders Reconnect ─────


class TestJsReplaySuite:
    """Extends the replay harness to the other panel surfaces. Each test
    drives the SHIPPED inline JS through a scenario (clicks/inputs) against
    recorded real-API answers and asserts the outcome via a sandbox
    verdict expression — same layer as the Reconnect pin.
    """

    @pytest.fixture(scope="class")
    def js_harness(self):
        node_exe = shutil.which("node")
        if not node_exe:
            pytest.skip("node not available; JS replay regression not run")
        linkedom_dir = Path(__file__).parent / "js_replay"
        node_modules = linkedom_dir / "node_modules"
        if not (node_modules / "linkedom").exists():
            npm = npm_exe()
            if not npm:
                pytest.skip("npm not available; JS replay regression not run")
            subprocess.run(
                [npm, "install", "--prefix", str(linkedom_dir), "--no-save", "linkedom"],
                capture_output=True, timeout=300, check=True,
            )
        return {"node": node_exe, "dir": linkedom_dir}

    def _run_replay(self, js_harness, answers: dict) -> dict:
        d = tmp_dir()
        client = TestClient(server.app)
        r = client.get("/")
        assert r.status_code == 200
        html_path = d / "replay_index.html"
        html_path.write_text(r.text, encoding="utf-8")
        result_path = d / "replay_result.json"
        answers = dict(answers)
        answers["resultPath"] = str(result_path)
        answers_path = d / "replay_answers.json"
        answers_path.write_text(json.dumps(answers), encoding="utf-8")
        proc = subprocess.run(
            [js_harness["node"], str(js_harness["dir"] / "replay_driver.js"),
             str(html_path), str(answers_path)],
            capture_output=True, text=True, timeout=120, cwd=str(js_harness["dir"]),
        )
        if result_path.exists():
            return json.loads(result_path.read_text(encoding="utf-8"))
        return {"threw": (proc.stdout + proc.stderr).strip() or "driver produced no verdict"}

    def test_sweep_now_button_posts_and_reports_counts(self, js_harness):
        """Scribble tab: Sweep Now → POST /api/sweep → status bar reports
        processed/tasks/threads/archived from the response body."""
        sweep_result = {"processed": 2, "tasks_created": 1, "threads_updated": 1, "archived": 2}
        verdict_expr = (
            "({ clickedAndReported: document.getElementById('status-region')"
            ".textContent.includes('2 processed') &&"
            " document.getElementById('status-region').textContent.includes('1 task(s)'),"
            " statusText: document.getElementById('status-region').textContent })"
        )
        verdict = self._run_replay(js_harness, {
            "responses": {
                "/api/integrations": {"connections": [], "available_services": []},
                "/api/approvals": [],
                "/api/settings": {},
                "/api/tasks": [],
                "/api/security/tier": {"tier": "semi_autonomous"},
                "/api/gateway/status": {},
                "/api/agents": [],
                "/api/agents/imported": [],
                "/api/runner/status": {"llm_configured": False},
                "/api/scribble": {"content": ""},
                "/api/memory/files": [],
                "/api/sweep POST": sweep_result,
            },
            "steps": [{"click": "#sweep-btn"}, {"waitForStatus": "Sweep done: 2 processed"}],
            "verdict": verdict_expr,
        })
        assert verdict.get("threw") is None, verdict
        assert verdict.get("stepError") is None, verdict
        assert verdict["clickedAndReported"] is True

    def test_memory_search_syncs_then_renders_results(self, js_harness):
        """Memory tab: Search → auto POST /api/memory/sync → GET search →
        resultcard with title + path + an Open button."""
        verdict_expr = (
            "({ matchCount: document.querySelectorAll('#memory-results .connection-card').length,"
            " firstTitle: (document.querySelector('#memory-results .task-title')||{}).textContent,"
            " openBtn: !!document.querySelector('#memory-results button'),"
            " statusAfter: document.getElementById('status-region').textContent })"
        )
        verdict = self._run_replay(js_harness, {
            "responses": {
                "/api/integrations": {"connections": [], "available_services": []},
                "/api/approvals": [],
                "/api/settings": {},
                "/api/tasks": [],
                "/api/security/tier": {"tier": "semi_autonomous"},
                "/api/gateway/status": {},
                "/api/agents": [],
                "/api/agents/imported": [],
                "/api/runner/status": {"llm_configured": False},
                "/api/scribble": {"content": ""},
                "/api/memory/sync POST": {"status": "synced", "files": 4},
                "/api/memory/search": [
                    {"id": 1, "path": "vault\\Journal\\2026-10.md", "title": "Journal 2026-10",
                     "updated_at": "2026-10-21T11:13:08", "relevance": 0.2}],
                "/api/memory/files": [
                    {"path": "vault\\Journal\\2026-10.md", "title": "Journal 2026-10",
                     "updated_at": "2026-10-21T11:13:08"}],
                "/api/memory/file": {"path": "vault\\Journal\\2026-10.md", "title": "2026-10",
                                     "content": "# Journal 2026-10\n"},
            },
            "steps": [
                {"set": ["memory-input", "dentist"]},
                {"click": "#memory-form button"},
                {"waitMs": 400},
            ],
            "verdict": verdict_expr,
        })
        assert verdict.get("threw") is None, verdict
        assert verdict.get("stepError") is None, verdict
        assert verdict["matchCount"] == 1
        assert verdict["firstTitle"] == "Journal 2026-10"
        assert verdict["openBtn"] is True

    def test_approvals_render_and_approve_reloads_empty(self, js_harness):
        """Pending Approvals: one pending card renders with Approve/Reject
        wired to handleApproval(id, action); clicking Approve POSTs the
        action, announces it, and reloads the (now empty) list."""
        verdict_expr = (
            "({ approveBtns: [...document.querySelectorAll('#approvals-list button')]"
            ".filter(b => b.textContent === 'Approve').length,"
            " rejectBtns: [...document.querySelectorAll('#approvals-list button')]"
            ".filter(b => b.textContent === 'Reject').length,"
            " statusText: document.getElementById('status-region').textContent,"
            " listAfter: document.getElementById('approvals-list').textContent })"
        )
        answers = {
            "responses": {
                "/api/security/tier": {"tier": "god_mode"},
                "/api/tasks": [],
                "/api/settings": {},
                "/api/settings POST": {"status": "saved"},
                "/api/runner/status": {"llm_configured": False},
                "/api/scribble": {"content": ""},
                "/api/memory/files": [],
                "/api/integrations": {"connections": [], "available_services": []},
                "/api/gateway/status": {"tier": "god_mode", "pending_approvals": 1,
                                        "telegram": {"configured": True},
                                        "slack": {"configured": False},
                                        "whatsapp": {"auth_exists": True}},
                "/api/agents": [],
                "/api/agents/imported": [],
                "/api/approvals/7/approve": {"status": "approved"},
            },
            "steps": [
                {"waitMs": 300},
                {"click": "#approvals-list .approval-actions button"},
                {"waitMs": 400},
            ],
            "verdict": verdict_expr,
        }
        # /api/approvals must answer the LOAD with one card and the RELOAD
        # (after the action) with none — the driver supports a response
        # queue suffix for exactly this.
        answers["responses"]["/api/approvals LOADS"] = [
            [{"id": 7, "action_type": "browser_action",
              "description": "Foreign-origin form submit blocked",
              "created_at": "2026-10-21 09:00:08", "status": "pending"}],
            [],
        ]
        verdict = self._run_replay(js_harness, answers)
        assert verdict.get("threw") is None, verdict
        assert verdict.get("stepError") is None, verdict
        # Rendering happened (before the action, the card had both buttons).
        assert "Approval approved." in verdict["statusText"]
        assert "No pending approvals" in verdict["listAfter"]

    def test_tier_save_updates_badge_and_status(self, js_harness):
        """Tier form: submit posts the select's value and both announce
        'Tier set to: …' and repaint the header badge."""
        verdict_expr = (
            "({ badge: document.querySelector('#tier-display .tier-badge').textContent,"
            " statusText: document.getElementById('status-region').textContent })"
        )
        verdict = self._run_replay(js_harness, {
            "responses": {
                "/api/security/tier": {"tier": "god_mode"},
                "/api/security/tier POST": {"tier": "god_mode"},
                "/api/tasks": [],
                "/api/settings": {},
                "/api/settings POST": {"status": "saved"},
                "/api/runner/status": {"llm_configured": False},
                "/api/scribble": {"content": ""},
                "/api/memory/files": [],
                "/api/integrations": {"connections": [], "available_services": []},
                "/api/gateway/status": {"tier": "god_mode", "pending_approvals": 0,
                                        "telegram": {"configured": False},
                                        "slack": {"configured": False},
                                        "whatsapp": {"auth_exists": False}},
                "/api/agents": [],
                "/api/agents/imported": [],
                "/api/approvals": [],
            },
            "steps": [
                {"set": ["tier-select", "god_mode"]},
                {"click": "#tier-form button"},
                {"waitForStatus": "Tier set to: god_mode"},
            ],
            "verdict": verdict_expr,
        })
        assert verdict.get("threw") is None, verdict
        assert verdict.get("stepError") is None, verdict
        assert verdict["badge"] == "god mode"
        assert "Tier set to: god_mode" in verdict["statusText"]

    def test_gateway_status_renders_tables_and_prefills_form(self, js_harness):
        """Gateways: status table renders tier/pending/channel badges from
        /api/gateway/status AND pre-fills the form fields from /api/settings."""
        verdict_expr = (
            "({ rows: [...document.querySelectorAll('#gateway-status tr')].length,"
            " tgRowBadges: [...(document.querySelectorAll('#gateway-status tr')[2] || {querySelectorAll: function(){return []}}).querySelectorAll('.badge')].map(b => b.textContent),"
            " tgToken: document.getElementById('tg-token').value,"
            " slackToken: document.getElementById('slack-token').value })"
        )
        verdict = self._run_replay(js_harness, {
            "responses": {
                "/api/security/tier": {"tier": "god_mode"},
                "/api/tasks": [],
                "/api/settings": {"telegram_bot_token": "123456:ABC-tok",
                                  "telegram_chat_whitelist": ["111", "222"],
                                  "slack_signing_secret": "sig-abc",
                                  "slack_bot_token": "xoxb-slack-tok"},
                "/api/runner/status": {"llm_configured": False},
                "/api/scribble": {"content": ""},
                "/api/memory/files": [],
                "/api/integrations": {"connections": [], "available_services": []},
                "/api/gateway/status": {"tier": "god_mode", "pending_approvals": 5,
                                        "telegram": {"configured": True},
                                        "slack": {"configured": True},
                                        "whatsapp": {"auth_exists": False}},
                "/api/agents": [],
                "/api/agents/imported": [],
                "/api/approvals": [],
            },
            "steps": [{"waitMs": 400}],
            "verdict": verdict_expr,
        })
        assert verdict.get("threw") is None, verdict
        assert verdict.get("stepError") is None, verdict
        assert verdict["rows"] == 5  # tier, pending, telegram, slack, whatsapp
        assert verdict["tgRowBadges"] == ["configured"]
        assert verdict["tgToken"] == "123456:ABC-tok"
        assert verdict["slackToken"] == "xoxb-slack-tok"

    def test_llm_save_reports_runner_activation_and_updates_idle_notice(self, js_harness):
        """Settings: save LLM key (server says runner_triggered) → status
        announcement + idle notice box re-checks /api/runner/status (which
        now reports configured)."""
        verdict_expr = (
            "({ statusText: document.getElementById('status-region').textContent,"
            " idleNoticeGone: document.getElementById('task-runner-status').textContent.length === 0 })"
        )
        verdict = self._run_replay(js_harness, {
            "responses": {
                "/api/integrations": {"connections": [], "available_services": []},
                "/api/approvals": [],
                "/api/settings": {},
                "/api/tasks": [],
                "/api/security/tier": {"tier": "semi_autonomous"},
                "/api/gateway/status": {},
                "/api/agents": [],
                "/api/agents/imported": [],
                "/api/runner/status": {"llm_configured": True, "provider": "openai", "model": "gpt-4o-mini"},
                "/api/scribble": {"content": ""},
                "/api/memory/files": [],
                "/api/settings POST": {"status": "saved", "rejected": [], "runner_triggered": True},
            },
            "steps": [
                {"set": ["llm-api-key", "sk-test"]},
                {"click": "#llm-save-btn"},
                {"waitForStatus": "Task runner activating"},
                {"waitMs": 250},
            ],
            "verdict": verdict_expr,
        })
        assert verdict.get("threw") is None, verdict
        assert verdict.get("stepError") is None, verdict
        assert "Task runner activating" in verdict["statusText"]
        assert verdict["idleNoticeGone"] is True


class TestConnectionsReconnect_JsReplay:
    """Serve the real index.html through FastAPI's TestClient, snapshot the
    CURRENT grouped `/api/integrations` payload shape with an expired
    connection, and replay it against the SHIPPED inline JavaScript running
    in a Node sandbox (linkedom + vm) — asserting the panel renders a
    Reconnect button for it. The Round 17.2 walkthrough regression (payload
    regrouped upstream, panel still mapping it flat, Reconnect silently
    gone) is pinned here at the exact layer it happened: real JS, real
    grouped payload.
    """

    @pytest.fixture(scope="class")
    def js_harness(self):
        node_exe = shutil.which("node")
        if not node_exe:
            pytest.skip("node not available; JS replay regression not run")
        linkedom_dir = Path(__file__).parent / "js_replay"
        node_modules = linkedom_dir / "node_modules"
        if not (node_modules / "linkedom").exists():
            subprocess.run(
                [npm_exe(), "install", "--prefix", str(linkedom_dir), "--no-save", "linkedom"],
                capture_output=True, timeout=300, check=True,
            )
        return {"node": node_exe, "dir": linkedom_dir}

    def test_grouped_payload_with_expired_connection_renders_reconnect(self, js_harness):
        client = TestClient(server.app)
        r = client.get("/")
        assert r.status_code == 200
        html_path = tmp_dir() / "replay_index.html"
        html_path.write_text(r.text, encoding="utf-8")

        past = "2026-08-16T21:04:43"  # the exact shape list_integrations emits
        grouped_payload = {
            "connections": [
                {"service": "google", "connections": [
                    {"label": "tracy", "user_id": "", "connected_at": "2026-08-15T10:00:00",
                     "expires_at": past, "scopes": ["email"], "status": "active"}],
                },
                {"service": "slack", "connections": [
                    {"label": "work", "user_id": "", "connected_at": "2026-09-01T10:00:00",
                     "expires_at": "2036-01-01T00:00:00", "scopes": ["chat"], "status": "active"}],
                },
            ],
            "available_services": ["zoom", "email"],
        }
        answers = {
            "resultPath": str(tmp_dir() / "replay_result.json"),
            "responses": {
                "/api/integrations GET": grouped_payload,
                "/api/approvals": [],
                "/api/settings": {},
                "/api/tasks": [],
                "/api/security/tier": {"tier": "semi_autonomous"},
                "/api/gateway/status": {},
                "/api/agents": [],
                "/api/agents/imported": [],
                "/api/runner/status": {"llm_configured": False},
                "/api/scribble": {"content": ""},
                "/api/memory/files": [],
                "/api/integrations/test": {"ok": True},
            },
        }
        answers_path = tmp_dir() / "replay_answers.json"
        answers_path.write_text(json.dumps(answers), encoding="utf-8")
        result_path = Path(answers["resultPath"])

        proc = subprocess.run(
            [js_harness["node"], str(js_harness["dir"] / "replay_driver.js"),
             str(html_path), str(answers_path)],
            capture_output=True, text=True, timeout=120, cwd=str(js_harness["dir"]),
        )
        if result_path.exists():
            verdict = json.loads(result_path.read_text(encoding="utf-8"))
        else:
            verdict = {"threw": proc.stdout + proc.stderr}

        assert verdict.get("threw") is None, f"inline JS threw: {verdict.get('threw')}"
        assert verdict["expiredCount"] >= 1, "expired card not detected by the real JS"
        assert verdict["reconnectCount"] >= 1, "Reconnect button not rendered by the real JS"
        assert any("tracy" in lbl for lbl in verdict["labels"]), f"label missing: {verdict['labels']}"
        assert "google" in verdict["firstReconnect"], f"wrong service wiring: {verdict['firstReconnect']}"


def npm_exe():
    return shutil.which("npm.cmd") or shutil.which("npm")


def tmp_dir():
    import tempfile
    d = Path(tempfile.gettempdir()) / "freehand_js_replay"
    d.mkdir(exist_ok=True)
    return d
