"""Calendars (Nov 2026): adding events, the person's own time, and short
results.

- create_calendar_event is a real tool: FreeHand couldn't add anything to
  a calendar before. It's asked for once, saying what and when.
- A time with no time zone is the person's own (it was UTC).
- Calendar results are trimmed: Google sent about 500 tokens per event.
- FreeHand's instructions say what day and time it is.
"""
import asyncio
import json
from zoneinfo import ZoneInfo

import pytest

from core import agent
from core.tools import integrations as it

SAST = ZoneInfo("Africa/Johannesburg")


@pytest.fixture(autouse=True)
def sast(monkeypatch):
    monkeypatch.setattr("core.scheduler.local_tz", lambda: SAST)


@pytest.fixture
def oc(monkeypatch):
    calls = []
    reply = {"value": {"ok": True, "data": {}}}

    def fake_execute(action_id, input_data, connection_name=None, timeout=10.0):
        calls.append({"action": action_id, "input": input_data, "label": connection_name})
        return reply["value"]
    monkeypatch.setattr("core.oauth.open_connector.execute_action", fake_execute)
    return calls, reply


GOOGLE_EVENT = {
    "id": "e1", "summary": "Dentist", "location": "Claremont", "description": "Check-up",
    "start": {"dateTime": "2026-11-20T10:00:00+02:00", "timeZone": "Africa/Johannesburg"},
    "end": {"dateTime": "2026-11-20T11:00:00+02:00", "timeZone": "Africa/Johannesburg"},
    "attendees": [{"email": "me@x.com", "self": True}, {"email": "sam@x.com"}],
    "htmlLink": "https://calendar.google.com/x", "reminders": {"useDefault": True},
    "conferenceData": {"entryPoints": [{"uri": "https://meet.google.com/abc"}] * 5}, "etag": "x" * 40,
}


def test_google_events_are_trimmed_to_what_matters():
    slim = it.slim_events("google", {"items": [GOOGLE_EVENT, {"id": "e2", "summary": "Holiday",
                                                             "start": {"date": "2026-12-24"}, "end": {"date": "2026-12-27"}}]})
    assert slim["events"][0] == {"id": "e1", "title": "Dentist", "start": "2026-11-20 10:00", "end": "2026-11-20 11:00",
                                 "where": "Claremont", "notes": "Check-up", "with": ["sam@x.com"]}
    assert slim["events"][1] == {"id": "e2", "title": "Holiday", "start": "2026-12-24", "end": "2026-12-27",
                                 "all_day": True}
    assert len(json.dumps(slim)) < len(json.dumps(GOOGLE_EVENT)) / 2


def test_outlook_times_are_shown_in_the_persons_own_time():
    slim = it.slim_events("microsoft", {"value": [{
        "id": "o1", "subject": "Board meeting", "isAllDay": False, "bodyPreview": "Agenda attached",
        "start": {"dateTime": "2026-11-20T08:00:00.0000000", "timeZone": "UTC"},
        "end": {"dateTime": "2026-11-20T09:30:00.0000000", "timeZone": "UTC"},
        "location": {"displayName": "Zoom"}, "attendees": [{"emailAddress": {"address": "lee@y.com"}}]}]})
    assert slim["events"][0] == {"id": "o1", "title": "Board meeting", "start": "2026-11-20 10:00",
                                 "end": "2026-11-20 11:30", "where": "Zoom", "notes": "Agenda attached",
                                 "with": ["lee@y.com"]}


def test_a_listed_window_without_a_zone_is_the_persons_own_time():
    start, end = it._default_window("2026-11-20T00:00", "2026-11-21T00:00")
    assert start.startswith("2026-11-19T22:00") and end.startswith("2026-11-20T22:00")


def test_adding_a_google_event_in_the_persons_time(oc):
    calls, reply = oc
    reply["value"] = {"ok": True, "data": GOOGLE_EVENT}
    out = asyncio.run(it.create_calendar_event("google", "Personal", "Dentist", "2026-11-20T10:00",
                                               location="Claremont", attendees="sam@x.com"))
    event = calls[0]["input"]["event"]
    assert event["start"] == {"dateTime": "2026-11-20T10:00:00+02:00"}
    assert event["end"] == {"dateTime": "2026-11-20T11:00:00+02:00"}  # an hour, by default
    assert event["attendees"] == [{"email": "sam@x.com"}] and event["location"] == "Claremont"
    assert out["added"] is True and out["title"] == "Dentist" and out["start"] == "2026-11-20 10:00"


def test_adding_an_outlook_event_sends_utc(oc):
    calls, reply = oc
    asyncio.run(it.create_calendar_event("microsoft", "Personal", "Call", "2026-11-20T10:00", duration_minutes=30))
    event = calls[0]["input"]["event"]
    assert event["start"] == {"dateTime": "2026-11-20T08:00:00", "timeZone": "UTC"}
    assert event["end"] == {"dateTime": "2026-11-20T08:30:00", "timeZone": "UTC"}


@pytest.mark.parametrize("kwargs, words", [
    ({"title": "", "start": "2026-11-20T10:00"}, "name and a start"),
    ({"title": "x", "start": "Friday at ten"}, "name and a start"),
    ({"title": "x", "start": "2026-11-20T10:00", "end": "2026-11-20T09:00"}, "end after it starts"),
])
def test_mistakes_are_said_plainly(oc, kwargs, words):
    out = asyncio.run(it.create_calendar_event("google", "Personal", **kwargs))
    assert words in out["error"]


def test_the_approval_says_what_and_when():
    text = agent.describe_action("create_calendar_event", {"title": "Dentist", "start": "2026-11-20T10:00",
                                                           "location": "Claremont"})
    assert text == "Add to your calendar: Dentist\nFriday 20 November 2026 at 10:00\nWhere: Claremont"


def test_adding_an_event_is_held_for_approval_then_added_once(oc, monkeypatch):
    from core import security
    calls, reply = oc
    security.set_tier(security.PermissionTier.SEMI_AUTONOMOUS.value)
    replies = iter([{"content": "", "tool_calls": [{"id": "c1", "type": "function", "function": {
        "name": "create_calendar_event", "arguments": json.dumps(
            {"service": "google", "label": "Personal", "title": "Dentist", "start": "2026-11-20T10:00"})}}]},
        {"content": "It's waiting for your approval."}])

    async def fake_llm(messages, tools=None, config=None):
        return next(replies)
    monkeypatch.setattr(agent, "call_llm", fake_llm)
    asyncio.run(agent.run_agent("Put the dentist in my diary on Friday at 10", source="selftest"))
    assert calls == []
    pending = security.get_pending_approvals()
    assert pending[0]["description"].startswith("Add to your calendar: Dentist")
    conn = security._get_conn()
    conn.execute("UPDATE approvals SET status = 'approved' WHERE id = ?", (pending[0]["id"],))
    conn.commit()
    conn.close()
    reply["value"] = {"ok": True, "data": GOOGLE_EVENT}
    out = asyncio.run(security.re_execute_approval(pending[0]["id"]))
    assert json.loads(out["result"]["content"])["added"] is True
    assert len(calls) == 1 and security.get_pending_approvals() == []


def test_freehand_knows_what_day_it_is():
    from core.agent_config import build_system_prompt
    first_lines = build_system_prompt().splitlines()[:3]
    assert any(line.startswith("Now: ") and "(Africa/Johannesburg)" in line for line in first_lines)


def test_team_members_can_be_allowed_to_add_events():
    from core import team
    assert team.TOOL_CHOICES["calendar_add"]["tools"] == ["create_calendar_event"]


def test_the_person_is_told_where_to_approve():
    assert "/approve 7" in agent._approval_hint("telegram", 7)
    web = agent._approval_hint("web", 7)
    assert "under Approvals" in web and "yes in the chat doesn't approve it" in web
