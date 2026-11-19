"""What a member may ask FreeHand to do (core/member_limits.py): not the
owner's browser or saved browser states, only their own documents, and a
change on a shared account waits for the owner."""
import asyncio
import contextlib
import json
from pathlib import Path

import pytest

from core import accounts, agent, agent_config, member_limits, people, security


THANDI = {"id": "thandi", "name": "Thandi", "role": "member", "telegram": ["555"]}


@contextlib.contextmanager
def as_person(pid):
    token = people.set_current(pid)
    try:
        yield
    finally:
        people.reset(token)


@pytest.fixture(autouse=True)
def office():
    s = agent_config._load_settings()
    s.update(owner_name="Tracy", people=[THANDI])
    agent_config._save_settings(s)
    security.set_tier("semi_autonomous")


@pytest.fixture
def llm(monkeypatch):
    replies = []

    async def fake_llm(messages, tools=None, config=None):
        reply = replies.pop(0) if replies else {"content": "Done."}
        return {**reply, "_usage": {"prompt_tokens": 1, "completion_tokens": 1}}

    monkeypatch.setattr(agent, "call_llm", fake_llm)
    ran, said = [], []

    async def fake_run_tool(name, args):
        ran.append(name)
        return {"content": "{}"}

    monkeypatch.setattr(agent, "_run_tool", fake_run_tool)
    return replies, ran


def _call(name, args):
    return {"content": "", "tool_calls": [{"id": "c1", "type": "function",
                                            "function": {"name": name, "arguments": json.dumps(args)}}]}


def _pending():
    with as_person("owner"):
        return security.get_pending_approvals()


SEND_SHARED = ("send_email", {"service": "google", "label": "info", "to": "x@example.com", "subject": "s", "body": "b"})


# ── Saved browser states and recordings are the owner's ──────────────

@pytest.mark.parametrize("tool", sorted(member_limits.OWNER_ONLY_TOOLS))
def test_a_member_cant_use_the_owners_browser_states_or_recordings(tool):
    with as_person("thandi"):
        assert "Only Tracy" in member_limits.refusal(tool, {})
    with as_person("owner"):
        assert member_limits.refusal(tool, {}) is None


def test_in_a_real_run_thandis_restore_is_refused_and_doesnt_run(llm):
    security.set_tier("full_access")
    replies, ran = llm
    replies.append(_call("restore_checkpoint", {"name": "bank"}))
    asyncio.run(agent.run_agent("log in to the bank", source="telegram", caller_id="555"))
    assert ran == []


# ── A member's browser is a clean one ────────────────────────────────

class FakeChromium:
    def __init__(self):
        self.used = []

    async def connect_over_cdp(self, url, timeout=0):
        self.used.append("owner's chrome")
        return object()

    async def launch(self, headless=True):
        self.used.append("clean")
        return object()


@pytest.fixture
def chromium(monkeypatch):
    from core.tools import browser
    fake = FakeChromium()

    async def fake_playwright():
        return type("P", (), {"chromium": fake})()

    monkeypatch.setattr(browser, "_playwright", fake_playwright)
    monkeypatch.setattr(browser, "_chrome_extension_bridge_port", lambda: 9223)
    monkeypatch.setattr(browser, "_get_chrome_cdp_endpoint", lambda: "ws://x")
    return fake


def test_a_members_browser_never_drives_the_owners_chrome(chromium):
    from core.tools import browser
    with as_person("thandi"):
        _, how = asyncio.run(browser._launch_browser())
    assert how == "sandboxed" and chromium.used == ["clean"]


def test_the_owners_browser_still_uses_their_chrome(chromium):
    from core.tools import browser
    with as_person("owner"):
        _, how = asyncio.run(browser._launch_browser())
    assert how == "chrome_extension"


# ── A member's documents stay in their own folder ────────────────────

def test_a_member_reads_and_writes_only_in_their_own_folder(tmp_path):
    from core.tools import office
    vault = Path(agent_config.VAULT_DIR)
    mine = vault / "people" / "thandi" / "notes.docx"
    owners = vault / "Report.docx"
    with as_person("thandi"):
        assert office.check_read_allowed(str(mine)) is None
        assert office.check_read_allowed(str(owners))
        assert office._check_write_permission(str(mine), ".docx")["allowed"]
        assert not office._check_write_permission(str(owners), ".docx")["allowed"]


def test_a_member_doesnt_get_the_owners_extra_folders(tmp_path):
    from core.tools import office
    s = agent_config._load_settings()
    s["allowed_read_dirs"] = [str(tmp_path / "Documents")]
    agent_config._save_settings(s)
    with as_person("thandi"):
        assert office.check_read_allowed(str(tmp_path / "Documents" / "salaries.xlsx"))
    with as_person("owner"):
        assert office.check_read_allowed(str(tmp_path / "Documents" / "salaries.xlsx")) is None


def test_the_owner_doesnt_reach_into_a_members_folder():
    from core.tools import office
    vault = Path(agent_config.VAULT_DIR)
    with as_person("owner"):
        assert office.check_read_allowed(str(vault / "Report.docx")) is None
        assert office.check_read_allowed(str(vault / "people" / "thandi" / "notes.docx"))
        assert not office._check_write_permission(str(vault / "people" / "thandi" / "x.docx"), ".docx")["allowed"]


# ── A change on a shared account waits for the owner ─────────────────

def test_a_members_send_from_the_shared_mailbox_waits_for_the_owner_even_at_full_access(llm):
    accounts.set_owner("oc:gmail:info", "shared")
    security.set_tier("full_access")
    replies, ran = llm
    replies.append(_call(*SEND_SHARED))
    asyncio.run(agent.run_agent("email x from info", source="telegram", caller_id="555"))
    assert ran == []
    pending = _pending()
    assert len(pending) == 1 and json.loads(pending[0]["payload"])[member_limits.NEEDS_OWNER] is True


def test_reading_the_shared_mailbox_needs_nobody(llm):
    accounts.set_owner("oc:gmail:info", "shared")
    replies, ran = llm
    replies.append(_call("read_email", {"service": "google", "label": "info"}))
    asyncio.run(agent.run_agent("anything in info?", source="telegram", caller_id="555"))
    assert ran == ["read_email"] and _pending() == []


def test_the_owner_sending_from_the_shared_mailbox_is_as_before(llm):
    accounts.set_owner("oc:gmail:info", "shared")
    security.set_tier("full_access")
    replies, ran = llm
    replies.append(_call(*SEND_SHARED))
    asyncio.run(agent.run_agent("email x from info"))
    assert ran == ["send_email"]


def test_the_member_cant_approve_it_but_can_cancel_it_and_the_owner_can_approve(llm):
    accounts.set_owner("oc:gmail:info", "shared")
    replies, _ = llm
    replies.append(_call(*SEND_SHARED))
    asyncio.run(agent.run_agent("email x from info", source="telegram", caller_id="555"))
    held = _pending()[0]["id"]
    with as_person("thandi"):
        with pytest.raises(ValueError, match="Only Tracy can approve"):
            security.handle_approval(held, "approve")
    with as_person("owner"):
        assert security.handle_approval(held, "approve")["status"] == "approved"


def test_a_member_cancelling_their_shared_account_step(llm):
    accounts.set_owner("oc:gmail:info", "shared")
    replies, _ = llm
    replies.append(_call(*SEND_SHARED))
    asyncio.run(agent.run_agent("email x from info", source="telegram", caller_id="555"))
    held = _pending()[0]["id"]
    with as_person("thandi"):
        assert security.handle_approval(held, "deny")["status"] == "rejected"


def test_approving_from_chat_is_refused_for_the_member_too(llm):
    accounts.set_owner("oc:gmail:info", "shared")
    replies, _ = llm
    replies.append(_call(*SEND_SHARED))
    asyncio.run(agent.run_agent("email x from info", source="telegram", caller_id="555"))
    held = _pending()[0]["id"]
    with as_person("thandi"):
        out = asyncio.run(security.approve_from_remote(held, "telegram", "555", person="thandi"))
    assert "Only Tracy" in out or "web console" in out
    assert _pending() and _pending()[0]["id"] == held


def test_a_members_change_on_their_own_account_doesnt_wait_for_the_owner(llm):
    accounts.set_owner("oc:gmail:thandi", "thandi")
    replies, ran = llm
    replies.append(_call("send_email", {"service": "google", "label": "thandi", "to": "x@example.com",
                                        "subject": "s", "body": "b"}))
    with as_person("thandi"):
        assert not member_limits.needs_owner_approval("send_email", {"service": "google", "label": "thandi"})
    asyncio.run(agent.run_agent("email x", source="telegram", caller_id="555"))
    assert not any(member_limits.NEEDS_OWNER in json.loads(a["payload"]) for a in _pending())
