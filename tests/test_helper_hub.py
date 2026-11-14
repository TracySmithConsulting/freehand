"""The FreeHand Helper, FreeHand's side (Nov 2026, stage 1: Look):
pairing, the connection, and the agent's computer_* tools."""
import asyncio
import json

import pytest
from fastapi.testclient import TestClient

import server
from core import agent, helper_hub


@pytest.fixture(autouse=True)
def fresh():
    helper_hub._codes.clear()
    helper_hub._bad.clear()
    helper_hub._connected.clear()
    if helper_hub._path().exists():
        helper_hub._path().unlink()
    yield
    helper_hub._connected.clear()


def keyed():
    return TestClient(server.app, headers={"X-API-Key": server.get_or_create_api_key()})


def test_pairing_swaps_a_one_use_code_for_a_token():
    code = keyed().post("/api/helper/pair-code").json()["code"]
    assert len(code) == 7 and code[3] == "-"
    c = TestClient(server.app)
    out = c.post("/api/helper/pair", json={"code": code, "name": "Shakila's PC"}).json()
    assert out["ok"] and out["token"] and out["name"] == "Shakila's PC"
    again = c.post("/api/helper/pair", json={"code": code, "name": "x"})
    assert again.status_code == 403 and "didn't work" in again.json()["detail"]
    saved = helper_hub._path().read_text(encoding="utf-8")
    assert out["token"] not in saved  # only a hash is kept
    assert helper_hub.device_for_token(out["token"])["name"] == "Shakila's PC"


def test_codes_can_not_be_guessed():
    c = TestClient(server.app)
    for _ in range(helper_hub.MAX_BAD_CODES):
        c.post("/api/helper/pair", json={"code": "000000", "name": "x"})
    code = helper_hub.new_code()["code"]
    out = c.post("/api/helper/pair", json={"code": code, "name": "x"})
    assert out.status_code == 403 and "Too many" in out.json()["detail"]


def test_pairing_codes_and_the_list_need_the_api_key():
    c = TestClient(server.app)
    assert c.post("/api/helper/pair-code").status_code == 401
    assert c.get("/api/helpers").status_code == 401


def test_a_wrong_token_is_turned_away():
    c = TestClient(server.app)
    with pytest.raises(Exception):
        with c.websocket_connect("/api/helper/connect", headers={"Authorization": "Bearer nope"}) as ws:
            ws.receive_json()


def test_a_helper_connects_over_the_websocket():
    """Without `with TestClient(...)`: that would run FreeHand's startup,
    which loads the real vault's add-on tools."""
    paired = helper_hub.pair(helper_hub.new_code()["code"], "Laptop")
    c = TestClient(server.app)
    with c.websocket_connect("/api/helper/connect", headers={"Authorization": "Bearer " + paired["token"]}) as ws:
        assert ws.receive_json() == {"type": "welcome", "name": "Laptop"}
        assert keyed().get("/api/helpers").json()["devices"][0]["connected"] is True
        assert helper_hub.device_now() == paired["device_id"]
    assert helper_hub.device_now() is None


def test_a_request_waits_for_the_helpers_answer():
    async def go():
        sent = []

        async def send(message):
            sent.append(message)
            # The Helper answers a moment later.
            asyncio.get_running_loop().call_later(0.01, helper_hub.received, "d1", {
                "type": "result", "id": message["id"], "ok": True,
                "data": {"window": "Untitled - Notepad", "tree": 'edit "Text editor"'}})

        async def close():
            pass
        helper_hub.connected({"id": "d1", "name": "Laptop"}, send, close)
        out = await helper_hub.request("d1", "look", {"window": "Notepad"}, "to read it")
        assert sent[0]["action"] == "look" and sent[0]["why"] == "to read it"
        return out
    assert asyncio.run(go())["data"]["window"] == "Untitled - Notepad"


def test_a_disconnect_ends_a_waiting_request():
    async def go():
        async def send(message):
            asyncio.get_running_loop().call_later(0.01, helper_hub.disconnected, "d2")

        async def close():
            pass
        helper_hub.connected({"id": "d2", "name": "PC"}, send, close)
        return await helper_hub.request("d2", "windows")
    assert "disconnected" in asyncio.run(go())["error"]


def test_removing_a_computer_ends_its_access():
    paired = helper_hub.pair(helper_hub.new_code()["code"], "Old PC")
    assert keyed().delete("/api/helpers/" + paired["device_id"]).json()["ok"]
    assert helper_hub.device_for_token(paired["token"]) is None
    assert keyed().delete("/api/helpers/" + paired["device_id"]).status_code == 404


def test_the_computer_tools_only_come_when_a_computer_is_connected_and_wanted(monkeypatch):
    seen = []

    async def fake_llm(messages, tools=None, config=None):
        seen.append([t["function"]["name"] for t in tools or []])
        return {"content": "ok"}
    monkeypatch.setattr(agent, "call_llm", fake_llm)
    asyncio.run(agent.run_agent("what's on my screen?", source="selftest"))
    assert "computer_look" not in seen[-1]  # nothing connected
    monkeypatch.setattr(helper_hub, "device_now", lambda person=None: "dev1")
    asyncio.run(agent.run_agent("what's on my screen?", source="selftest"))
    assert "computer_look" in seen[-1]
    asyncio.run(agent.run_agent("what's the capital of France?", source="selftest", caller_id="someone-else"))
    assert "computer_look" not in seen[-1]  # not about the computer


def test_a_screenshot_goes_to_the_model_as_an_image(monkeypatch):
    monkeypatch.setattr(helper_hub, "device_now", lambda person=None: "dev1")

    async def fake_request(device, action, args=None, why=""):
        return {"ok": True, "data": {"window": "Paint", "image": "data:image/jpeg;base64,AAAA"}}
    monkeypatch.setattr(helper_hub, "request", fake_request)
    sent = []
    replies = iter([
        {"content": "", "tool_calls": [{"id": "s1", "type": "function", "function": {
            "name": "computer_screenshot", "arguments": json.dumps({"window": "Paint", "why": "to see the buttons"})}}]},
        {"content": "The third button is Fill."},
    ])

    async def fake_llm(messages, tools=None, config=None):
        sent.append(list(messages))
        return dict(next(replies))
    monkeypatch.setattr(agent, "call_llm", fake_llm)
    out = asyncio.run(agent.run_agent("look at the buttons in Paint on my screen", source="selftest"))
    assert out["text"] == "The third button is Fill."
    image_msg = sent[1][-1]
    assert image_msg["role"] == "user" and image_msg["content"][1]["image_url"]["url"].startswith("data:image/jpeg")


def test_no_computer_means_a_clear_message():
    out = asyncio.run(helper_hub.run_tool("computer_look", {"why": "x"}))
    assert "No computer is connected" in out["content"]
