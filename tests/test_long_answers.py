"""Answers too long for the model's limit (Nov 2026). Chapter 5's edits
needed about 4,800 tokens and the model was allowed 4,096, so the answer
came back empty, FreeHand took that as the end, and reported "nothing
edited yet" every time. Now a cut-off answer is asked for again with more
room, and if it still doesn't fit, the model is told to do it in parts."""
import asyncio
import json

import pytest

pytest.importorskip("docx")

from docx import Document  # noqa: E402

from core import agent, documents as d  # noqa: E402


class FakeResponse:
    def __init__(self, body):
        self.status = 200
        self._body = body

    async def json(self):
        return self._body

    async def text(self):
        return json.dumps(self._body)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class FakeSession:
    sent = []
    answers = []

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def post(self, url, json=None, headers=None, timeout=None):
        FakeSession.sent.append(json)
        return FakeResponse(FakeSession.answers.pop(0))


@pytest.fixture
def service(monkeypatch):
    FakeSession.sent, FakeSession.answers = [], []
    monkeypatch.setattr(agent.aiohttp, "ClientSession", FakeSession)
    monkeypatch.setattr("core.agent_config.resolve_llm_endpoint", lambda cfg: ("http://model", {}))
    return FakeSession


def cut(tokens=4096, reason="length"):
    return {"choices": [{"message": {"content": ""}, "finish_reason": reason}],
            "usage": {"prompt_tokens": 10, "completion_tokens": tokens}}


def edit_call():
    return {"choices": [{"message": {"content": "", "tool_calls": [{"id": "e1", "type": "function", "function": {
        "name": "document_edit", "arguments": "{}"}}]}, "finish_reason": "tool_calls"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 4800}}


@pytest.mark.parametrize("first", [cut(), cut(reason="stop")])  # said, or just empty after using it all
def test_a_cut_off_answer_is_asked_for_again_with_more_room(service, first):
    service.answers = [first, edit_call()]
    msg = asyncio.run(agent.call_llm([{"role": "user", "content": "edit"}],
                                     config={"model": "m", "max_tokens": 4096}))
    assert [s["max_tokens"] for s in service.sent] == [4096, agent.LONG_ANSWER_TOKENS]
    assert msg["tool_calls"][0]["function"]["name"] == "document_edit" and "_cut_off" not in msg


def test_a_short_empty_answer_is_not_cut_off(service):
    service.answers = [cut(tokens=12, reason="stop")]
    msg = asyncio.run(agent.call_llm([{"role": "user", "content": "hi"}], config={"model": "m", "max_tokens": 4096}))
    assert len(service.sent) == 1 and "_cut_off" not in msg


def test_still_too_long_means_smaller_pieces(monkeypatch):
    calls = []

    async def model(messages, tools=None, config=None):
        calls.append(messages[-1]["content"])
        if len(calls) == 1:
            return {"content": "", "_cut_off": True, "_usage": {}}
        return {"content": "Done in parts.", "_usage": {}}
    monkeypatch.setattr(agent, "call_llm", model)
    out = asyncio.run(agent.run_agent("Make all the Chapter 5 changes"))
    assert out["text"] == "Done in parts."
    assert "at most about 15 changes per document_edit call" in calls[1]


def test_insert_after_with_a_number_means_after_that_paragraph(tmp_path, monkeypatch):
    monkeypatch.setattr(d, "folder", lambda: tmp_path)
    doc = Document()
    for t in ("One", "Two", "Three"):
        doc.add_paragraph(t)
    doc.save(str(tmp_path / "c.docx"))
    out = d.edit("c.docx", [{"insert_after": 1, "text": "Illustration: a caption"}])
    assert out["changes"] == ["a paragraph added after 1"]
    assert [p.text for p in Document(str(tmp_path / out["saved_as"])).paragraphs] == [
        "One", "Two", "Illustration: a caption", "Three"]


def test_a_document_part_reaches_the_model_whole():
    source = open(agent.__file__, encoding="utf-8").read()
    assert "WORK_RESULT_LLM_CAP = 40000" in source and d.PART_CHARS < 40000
    assert "cap = WORK_RESULT_LLM_CAP if tool_name in WORK_TOOLS else TOOL_RESULT_LLM_CAP" in source
