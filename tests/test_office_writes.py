"""Agent document writes (Nov 2026).

Writes need the tool's own extension and must land in the vault or
settings.allowed_write_dirs. One agent-level approval is the only gate —
there is no second, unreleasable "write_file" approval any more.
"""
import asyncio
import json
from pathlib import Path

import pytest

from core import agent, agent_config, security
from core.tools import office


def _vault() -> Path:
    return Path(agent_config.VAULT_DIR)


def test_write_into_vault_succeeds():
    target = _vault() / "out" / "report.docx"
    result = office.write_docx(str(target), "Report", ["hello"])
    assert "error" not in result, result
    assert target.exists()


@pytest.mark.parametrize("writer,good_ext,args", [
    (office.write_docx, ".docx", ("T", ["x"])),
    (office.write_xlsx, ".xlsx", ({"Sheet1": [["a"]]},)),
    (office.write_pptx, ".pptx", ([{"title": "t", "content": ["x"]}],)),
])
def test_wrong_extension_is_refused(writer, good_ext, args):
    for name in ("settings.json", "encryption.key", "server.py", "notes" + good_ext + ".txt"):
        target = _vault() / name
        result = writer(str(target), *args)
        assert result.get("error") == "Permission denied", (name, result)
        assert good_ext in result["message"]
        assert not target.exists()


def test_cannot_overwrite_project_code(tmp_path, monkeypatch):
    # Even a .docx in the project folder (outside the vault) is refused now;
    # the old rule allowed anything inside the project root.
    monkeypatch.setattr(office, "PROJECT_ROOT", tmp_path)
    target = tmp_path / "core" / "evil.docx"
    result = office.write_docx(str(target), "T", ["x"])
    assert result["error"] == "Permission denied"
    assert "not allowed" in result["message"]
    assert not target.exists()


def test_outside_roots_refused_without_creating_an_approval(tmp_path):
    target = tmp_path / "elsewhere" / "a.docx"
    result = office.write_docx(str(target), "T", ["x"])
    assert result["approval_id"] is None
    assert security.get_pending_approvals() == []
    assert not target.exists()


def test_sibling_prefix_folder_refused(tmp_path):
    target = tmp_path / (Path(agent_config.VAULT_DIR).name + "2") / "a.docx"
    assert office.write_docx(str(target), "T", ["x"])["error"] == "Permission denied"


def test_allowed_write_dirs_extends_roots(tmp_path):
    extra = tmp_path / "Documents"
    agent_config.SETTINGS_PATH.write_text(json.dumps({"allowed_write_dirs": [str(extra)]}))
    target = extra / "a.docx"
    assert "error" not in office.write_docx(str(target), "T", ["x"])
    assert target.exists()


def test_one_approval_is_enough_to_write(monkeypatch):
    """Semi-autonomous: the agent holds write_docx for approval; approving
    it in the console writes the file (it used to trip a second approval
    for paths outside the project folder, which could never be released)."""
    security.set_tier("semi_autonomous")
    target = _vault() / "approved.docx"
    replies = iter([
        {"tool_calls": [{"id": "t1", "function": {
            "name": "write_docx",
            "arguments": json.dumps({"path": str(target), "title": "T", "content": ["x"]})}}]},
        {"content": "Waiting for approval."},
    ])

    async def fake_llm(messages, tools=None, config=None):
        return next(replies)

    monkeypatch.setattr(agent, "call_llm", fake_llm)
    asyncio.run(agent.run_agent("write it"))
    (row,) = security.get_pending_approvals()
    assert not target.exists()

    security.handle_approval(row["id"], "approve")
    outcome = asyncio.run(security.re_execute_approval(row["id"]))
    assert outcome["re_executed"] is True
    assert target.exists()
    assert security.get_pending_approvals() == []
