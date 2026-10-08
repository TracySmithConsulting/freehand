"""Round 15 slice 3 — close the loop: approving a held destructive action
re-executes that exact tool call (source-gated).

The force-confirm gate (commits 4803f68 / 5d10cc4) HOLDS a destructive OC
tool at GOD_MODE and creates a pending approval. But approving it was a dead
action: handle_approval only flipped the DB status, so the out-of-band
buttons (web /approve, Telegram, Slack) were decorative. This slice adds
re_execute_approval(): when an approved destructive action is approved, re-drive
the exact tool + args it stored.

Source-gated (the security decision): only LOCAL-originating calls re-execute.
A github.delete_repo held at a remote gateway (telegram/slack/whatsapp) stays
veto/record-only — a remote /approve button must not fire a destructive action.

These are RED tests; they fail until security.re_execute_approval lands.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import security, agent  # noqa: E402
from core.database import init_db  # noqa: E402


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    db = tmp_path / "agent.db"
    monkeypatch.setattr("core.database.DB_PATH", db)
    monkeypatch.setattr("core.security.DB_PATH", db)
    init_db()
    return db


def _insert_approved(db, payload, status="approved") -> int:
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    cur = conn.execute(
        "INSERT INTO approvals (action_type, description, payload, status) "
        "VALUES ('oauth_access', 'Destructive tool call: X', ?, ?)",
        (json.dumps(payload), status),
    )
    aid = cur.lastrowid
    conn.commit()
    conn.close()
    return aid


# ── re_execute_approval: local source re-runs the exact held tool ─────


class TestReExecuteApproval:
    def test_local_source_re_executes(self, tmp_db, monkeypatch):
        """A locally-originated destructive call, once approved, re-runs
        with the SAME tool + args it was held with."""
        executed = {}
        async def fake_execute_tool(name, args):
            executed["name"] = name
            executed["args"] = args
            return {"content": "ran"}
        monkeypatch.setattr(agent, "execute_tool", fake_execute_tool)

        aid = _insert_approved(
            tmp_db,
            {"tool": "oc_github_default_delete_repo",
             "args": {"repo": "trasles16-ux/speechcraft-audio"},
             "source": "cli"},
        )
        result = asyncio.run(security.re_execute_approval(aid))
        assert result["re_executed"] is True
        assert result["tool"] == "oc_github_default_delete_repo"
        assert executed.get("name") == "oc_github_default_delete_repo"
        assert executed.get("args") == {"repo": "trasles16-ux/speechcraft-audio"}

    def test_remote_source_stays_veto(self, tmp_db, monkeypatch):
        """The security decision: a remote-gateway origin must NOT re-execute
        on /approve — the button records the approval but does not fire the
        destructive action."""
        called = {"n": 0}
        async def fake_execute_tool(name, args):
            called["n"] += 1
            return {"content": "should-not-run"}
        monkeypatch.setattr(agent, "execute_tool", fake_execute_tool)

        for remote in ("telegram", "slack", "whatsapp"):
            aid = _insert_approved(
                tmp_db,
                {"tool": "oc_github_default_delete_repo",
                 "args": {"repo": "x/y"}, "source": remote},
            )
            result = asyncio.run(security.re_execute_approval(aid))
            assert result["re_executed"] is False
            assert "remote" in result.get("reason", "").lower()
        assert called["n"] == 0, "a remote /approve fired a destructive tool"

    def test_rejects_unapproved_or_missing(self, tmp_db, monkeypatch):
        """Nothing to re-execute unless the approval exists AND is approved."""
        executed = {"n": 0}
        async def fake_execute_tool(name, args):
            executed["n"] += 1
            return {"content": "nope"}
        monkeypatch.setattr(agent, "execute_tool", fake_execute_tool)

        # status still 'pending' (not approved yet) -> not re-executed
        aid_pending = _insert_approved(
            tmp_db, {"tool": "oc_github_default_delete_repo",
                     "args": {}, "source": "cli"}, status="pending")
        r1 = asyncio.run(security.re_execute_approval(aid_pending))
        assert r1["re_executed"] is False

        # nonexistent id -> not re-executed
        r2 = asyncio.run(security.re_execute_approval(99999))
        assert r2["re_executed"] is False
        assert executed["n"] == 0

    def test_payload_without_tool_is_not_executed(self, tmp_db, monkeypatch):
        executed = {"n": 0}
        async def fake_execute_tool(name, args):
            executed["n"] += 1
            return {"content": "nope"}
        monkeypatch.setattr(agent, "execute_tool", fake_execute_tool)

        aid = _insert_approved(tmp_db, {"args": {}, "source": "cli"})  # no tool key
        result = asyncio.run(security.re_execute_approval(aid))
        assert result["re_executed"] is False
        assert executed["n"] == 0
