"""Tests for round-4 fixes (Aug 2026):
- B2: list_available_tools() is registry-driven (24 tools visible)
- R10: sync_vault_to_sqlite skips 00_Scribble.md and .sweep_state.json
- R11: parse_skills enforces project-root boundary (rejects symlink escape)
- R13: sync_vault_to_sqlite end-to-end
- R15: office _is_within_workspace boundary check
"""

import sys
import os
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, r"C:\Users\trace\Documents\Default Project")

from core import agent_config
from core.memory import sync_vault_to_sqlite, search_memory, parse_skills
from core.tools.office import _is_within_workspace, write_docx, read_docx
# NOTE: office tools live in core/tools/office.py — there is no
# core/office.py module. The first import above is the canonical path.


# ── B2: list_available_tools is registry-driven ───────────────────────

class TestListAvailableTools:
    def test_every_registry_tool_has_a_schema(self):
        """No tool should be in TOOL_REGISTRY without a matching TOOL_SCHEMAS entry."""
        reg = set(agent_config.TOOL_REGISTRY.keys())
        sch = set(agent_config.TOOL_SCHEMAS.keys())
        missing = reg - sch
        assert not missing, f"Tools in registry but no schema: {missing}"

    def test_every_schema_tool_is_in_registry(self):
        """No tool should be in TOOL_SCHEMAS without a matching TOOL_REGISTRY entry."""
        reg = set(agent_config.TOOL_REGISTRY.keys())
        sch = set(agent_config.TOOL_SCHEMAS.keys())
        orphans = sch - reg
        assert not orphans, f"Schemas without registry entries: {orphans}"

    def test_all_24_tools_visible_to_llm(self):
        """The LLM should see exactly the tools in TOOL_REGISTRY."""
        tools = agent_config.list_available_tools()
        names = {t["function"]["name"] for t in tools}
        assert names == set(agent_config.TOOL_REGISTRY.keys())
        assert len(tools) == 24

    def test_write_tools_visible(self):
        """The previously-dead write tools (post_to_facebook etc.) are now visible."""
        tools = {t["function"]["name"] for t in agent_config.list_available_tools()}
        # The 10 previously-dead tools
        previously_dead = [
            "read_sheet_range",
            "create_github_issue",
            "create_github_pull_request",
            "list_zoom_recordings",
            "schedule_zoom_meeting",
            "list_facebook_pages",
            "post_to_facebook",
            "list_instagram_accounts",
            "post_to_instagram",
            "list_connections",
        ]
        for tool in previously_dead:
            assert tool in tools, f"{tool} should be visible to the LLM"

    def test_schema_has_required_fields(self):
        """Every schema must have description + parameters dict with 'type': 'object'."""
        for name, schema in agent_config.TOOL_SCHEMAS.items():
            assert "description" in schema, f"{name} missing description"
            assert "parameters" in schema, f"{name} missing parameters"
            assert schema["parameters"].get("type") == "object", \
                f"{name} parameters must be type=object"
            assert "properties" in schema["parameters"], \
                f"{name} parameters must have properties"
            assert "required" in schema["parameters"], \
                f"{name} parameters must declare required fields (even if [])"


# ── R10: Skip scratchpad in vault sync ───────────────────────────────

class TestSyncVaultSkipsScratchpad:
    def _make_vault(self, tmp_path):
        """Build a minimal vault with a scribble + a real note."""
        vault = tmp_path / "vault"
        vault.mkdir()
        (vault / "00_Scribble.md").write_text(
            "# 00_Scribble\n\n- [ ] TODO this is a scratchpad\n",
            encoding="utf-8",
        )
        (vault / "notes.md").write_text(
            "# Notes\n\nReal knowledge here.\n",
            encoding="utf-8",
        )
        (vault / ".sweep_state.json").write_text(
            '{"processed_lines": [1, 2, 3]}',
            encoding="utf-8",
        )
        return vault

    def test_scribble_not_synced_to_memory(self, tmp_path, monkeypatch):
        from core.database import init_db
        vault = self._make_vault(tmp_path)
        db_path = tmp_path / "agent.db"
        from core import memory as mem_mod
        monkeypatch.setattr(mem_mod, "VAULT_DIR", vault)
        monkeypatch.setattr(mem_mod, "PROJECT_ROOT", tmp_path)
        monkeypatch.setattr("core.memory.DB_PATH", db_path)
        monkeypatch.setattr("core.database.DB_PATH", db_path)
        init_db()

        count = sync_vault_to_sqlite()
        # Only notes.md should be synced (1 file), not the scribble or sweep state
        assert count == 1

        # Verify the scribble is NOT in the memories table
        conn = sqlite3.connect(str(db_path))
        rows = conn.execute(
            "SELECT path FROM memories WHERE path LIKE '%Scribble%'"
        ).fetchall()
        assert len(rows) == 0, "00_Scribble.md should not be indexed"

        # And notes.md IS indexed
        rows = conn.execute(
            "SELECT path FROM memories WHERE path LIKE '%notes.md'"
        ).fetchall()
        assert len(rows) == 1

        conn.close()


# ── R11: parse_skills boundary enforcement ────────────────────────────

class TestParseSkillsBoundary:
    def test_symlink_skill_escaping_root_is_rejected(self, tmp_path, monkeypatch):
        """A symlink inside skills/ pointing outside should be rejected."""
        if not hasattr(os, "symlink"):
            pytest.skip("symlink not available on this platform")

        skills_root = tmp_path / "skills"
        skills_root.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "SKILL.md").write_text("# Outside\nbody\n", encoding="utf-8")

        evil_link = skills_root / "evil"
        try:
            os.symlink(outside, evil_link, target_is_directory=True)
        except OSError as e:
            # Windows requires admin privileges for symlinks. Skip the test
            # rather than fail — the production code path is the same.
            pytest.skip(f"symlink creation requires elevated privileges on this platform: {e}")

        # parse_skills takes skills_dir as an explicit argument, so we
        # don't need to monkey-patch PROJECT_ROOT.
        result = parse_skills(skills_dir=skills_root)
        # The "evil" symlink must NOT be parsed
        assert all("outside" not in s["path"] for s in result), \
            f"Evil symlink should have been rejected, got: {result}"

    def test_valid_skill_inside_root_is_accepted(self, tmp_path):
        skills_root = tmp_path / "skills"
        skills_root.mkdir()
        skill_dir = skills_root / "my-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\nname: my-skill\ndescription: A test skill\n---\nbody\n",
            encoding="utf-8",
        )

        # parse_skills's relative_to(PROJECT_ROOT) requires PROJECT_ROOT to
        # be an ancestor of skill_md. Patch both core.memory.PROJECT_ROOT
        # AND core.tools.office-equivalent so resolve() matches.
        from core import memory as mem_mod
        import core.memory
        original = mem_mod.PROJECT_ROOT
        mem_mod.PROJECT_ROOT = tmp_path
        try:
            result = parse_skills(skills_dir=skills_root)
        finally:
            mem_mod.PROJECT_ROOT = original

        assert len(result) == 1
        assert result[0]["name"] == "my-skill"

    def test_skill_md_outside_skill_dir_is_rejected(self, tmp_path):
        """A SKILL.md created via a symlink that points to a file outside the skill dir is rejected."""
        if not hasattr(os, "symlink"):
            pytest.skip("symlink not available")

        skills_root = tmp_path / "skills"
        skills_root.mkdir()
        skill_dir = skills_root / "skill-x"
        skill_dir.mkdir()

        # Create a SKILL.md outside, symlink it into skill-x
        outside_md = tmp_path / "outside_skill.md"
        outside_md.write_text(
            "---\nname: evil\n---\nbody\n",
            encoding="utf-8",
        )
        evil_skill = skill_dir / "SKILL.md"
        try:
            os.symlink(outside_md, evil_skill)
        except OSError as e:
            pytest.skip(f"symlink creation requires elevated privileges: {e}")

        result = parse_skills(skills_dir=skills_root)
        # The evil skill should be rejected (SKILL.md outside skill_dir)
        assert len(result) == 0


# ── R13: sync_vault_to_sqlite end-to-end ──────────────────────────────

class TestSyncVaultToSqlite:
    def test_sync_creates_memories_rows(self, tmp_path, monkeypatch):
        from core.database import init_db
        vault = tmp_path / "vault"
        vault.mkdir()
        (vault / "a.md").write_text("# Alpha\nFirst content here.\n", encoding="utf-8")
        (vault / "b.md").write_text("# Beta\nSecond content here.\n", encoding="utf-8")
        (vault / "c.md").write_text("# Gamma\nThird content here.\n", encoding="utf-8")
        db_path = tmp_path / "agent.db"
        from core import memory as mem_mod
        # Patch BOTH VAULT_DIR (so we scan the test vault) and
        # PROJECT_ROOT (so relative_to() works) to point at tmp_path.
        monkeypatch.setattr(mem_mod, "VAULT_DIR", vault)
        monkeypatch.setattr(mem_mod, "PROJECT_ROOT", tmp_path)
        monkeypatch.setattr("core.memory.DB_PATH", db_path)
        monkeypatch.setattr("core.database.DB_PATH", db_path)
        init_db()

        count = sync_vault_to_sqlite()
        assert count == 3

        conn = sqlite3.connect(str(db_path))
        rows = conn.execute("SELECT title, content FROM memories").fetchall()
        assert len(rows) == 3
        titles = {r[0] for r in rows}
        assert titles == {"Alpha", "Beta", "Gamma"}
        conn.close()

    def test_sync_is_idempotent(self, tmp_path, monkeypatch):
        """Running sync twice updates existing rows rather than duplicating."""
        from core.database import init_db
        vault = tmp_path / "vault"
        vault.mkdir()
        (vault / "a.md").write_text("# Alpha\nFirst content.\n", encoding="utf-8")
        db_path = tmp_path / "agent.db"
        from core import memory as mem_mod
        monkeypatch.setattr(mem_mod, "VAULT_DIR", vault)
        monkeypatch.setattr(mem_mod, "PROJECT_ROOT", tmp_path)
        monkeypatch.setattr("core.memory.DB_PATH", db_path)
        monkeypatch.setattr("core.database.DB_PATH", db_path)
        init_db()

        sync_vault_to_sqlite()
        # Modify the file
        (vault / "a.md").write_text("# Alpha\nUpdated content.\n", encoding="utf-8")
        sync_vault_to_sqlite()
        conn = sqlite3.connect(str(db_path))
        rows = conn.execute("SELECT content FROM memories").fetchall()
        assert len(rows) == 1  # No duplicates
        assert "Updated" in rows[0][0]
        conn.close()

    def test_extracts_h1_as_title(self, tmp_path, monkeypatch):
        from core.database import init_db
        vault = tmp_path / "vault"
        vault.mkdir()
        (vault / "doc.md").write_text(
            "# My Important Note\n\nSome body text.\n",
            encoding="utf-8",
        )
        db_path = tmp_path / "agent.db"
        from core import memory as mem_mod
        monkeypatch.setattr(mem_mod, "VAULT_DIR", vault)
        monkeypatch.setattr(mem_mod, "PROJECT_ROOT", tmp_path)
        monkeypatch.setattr("core.memory.DB_PATH", db_path)
        monkeypatch.setattr("core.database.DB_PATH", db_path)
        init_db()

        sync_vault_to_sqlite()
        conn = sqlite3.connect(str(db_path))
        rows = conn.execute("SELECT title, content FROM memories").fetchall()
        conn.close()
        # Filter to just our test doc to avoid picking up any real-vault noise
        titles = [r[0] for r in rows if "Important" in r[1]]
        assert "My Important Note" in titles, f"Expected our doc title, got: {titles}"


# ── R15: office _is_within_workspace ─────────────────────────────────

class TestOfficeWorkspaceBoundary:
    def test_path_inside_project_root_is_within_workspace(self, tmp_path, monkeypatch):
        # _is_within_workspace uses PROJECT_ROOT from core.security at
        # import time. Patch the office module's view of it.
        from core import tools
        from core.tools import office
        monkeypatch.setattr(office, "PROJECT_ROOT", tmp_path)

        inside = tmp_path / "subdir" / "file.docx"
        assert _is_within_workspace(str(inside)) is True

    def test_path_outside_project_root_is_outside_workspace(self, tmp_path, monkeypatch):
        from core.tools import office
        monkeypatch.setattr(office, "PROJECT_ROOT", tmp_path)

        # C:\Windows is outside tmp_path on Windows
        outside = "C:/Windows/System32/drivers/etc/hosts"
        assert _is_within_workspace(outside) is False

    def test_path_traversal_is_rejected(self, tmp_path, monkeypatch):
        from core.tools import office
        monkeypatch.setattr(office, "PROJECT_ROOT", tmp_path)

        # ../../etc/passwd should resolve outside
        evil = str(tmp_path / ".." / ".." / "etc" / "passwd")
        assert _is_within_workspace(evil) is False

    def test_write_docx_outside_workspace_returns_error(self, tmp_path, monkeypatch):
        """write_docx must refuse to write outside the project root."""
        from core.tools import office
        monkeypatch.setattr(office, "PROJECT_ROOT", tmp_path)

        # Outside the project root
        outside_dir = tmp_path.parent / "outside_evil"
        outside_dir.mkdir(exist_ok=True)
        evil_path = str(outside_dir / "evil.docx")

        result = write_docx(evil_path, "evil", ["body"])
        assert "error" in result
        # The file must NOT have been created
        assert not Path(evil_path).exists()