"""Google and Microsoft run through OpenConnector (Nov 2026).

FreeHand's email, calendar and Sheets tools keep their names and their
service/label arguments, but call OpenConnector actions instead of the
Google and Microsoft APIs with broker tokens. The label is the
OpenConnector connection name.
"""
import asyncio

import pytest

from core.oauth import broker, open_connector
from core.oauth.providers import CONNECTORS
from core.tools import integrations as it


@pytest.fixture
def oc(monkeypatch):
    calls = []
    reply = {"value": {"ok": True, "data": {"messages": [{"id": "1"}]}}}

    def fake_execute(action_id, input_data, connection_name=None, timeout=10.0):
        calls.append({"action": action_id, "input": input_data, "label": connection_name})
        return reply["value"]

    monkeypatch.setattr(open_connector, "execute_action", fake_execute)
    monkeypatch.setattr(it, "_check_write_permission", lambda *a, **k: {"allowed": True})
    return calls, reply


def run(coro):
    return asyncio.run(coro)


def test_gmail_read_uses_the_label_as_connection_name(oc):
    calls, _ = oc
    out = run(it.read_email("google", "work", "is:unread"))
    assert out == {"messages": [{"id": "1"}]}
    assert calls == [{"action": "gmail.fetch_emails", "label": "work",
                      "input": {"query": "is:unread", "maxResults": 10, "detail": "summary"}}]


def test_outlook_send_really_sends_to_every_recipient(oc):
    """The old code saved Outlook mail to the Outbox, which never sends."""
    calls, reply = oc
    reply["value"] = {"ok": True, "data": {"success": True}}
    out = run(it.send_email("microsoft", "default", "a@x.com; b@x.com", "Hi", "Body"))
    assert out["sent"] is True
    assert calls[0]["action"] == "outlook.send_email"
    assert calls[0]["input"]["toRecipients"] == ["a@x.com", "b@x.com"]


def test_gmail_send_has_no_fake_sender(oc):
    calls, reply = oc
    reply["value"] = {"ok": True, "data": {"messageId": "m1"}}
    run(it.send_email("google", "default", "a@x.com, b@x.com", "Hi", "Body"))
    sent = calls[0]["input"]
    assert calls[0]["action"] == "gmail.send_email"
    assert sent["to"] == "a@x.com" and sent["extraRecipients"] == ["b@x.com"]
    assert "sender@example.com" not in str(sent)


def test_send_still_needs_approval(monkeypatch):
    monkeypatch.setattr(it, "_check_write_permission", lambda *a, **k: {"allowed": False, "id": 3})
    out = run(it.send_email("google", "default", "a@x.com", "Hi", "Body"))
    assert "Approval required" in out["error"]


def test_not_connected_says_where_to_connect(oc, monkeypatch):
    _, reply = oc
    monkeypatch.setenv("OOMOL_CONNECT_CONSOLE_URL", "http://localhost:3001")
    reply["value"] = {"ok": False, "error": {"code": "connection_not_found", "message": "x"}}
    out = run(it.list_calendar_events("google", "tracy"))
    assert "No Google Calendar account called 'tracy'" in out["error"]
    # FreeHand's own Connect, not OpenConnector's admin page
    assert "open Connections" in out["error"]
    assert out["connect_url"] == "/api/integrations/googlecalendar/authorize?label=tracy"


def test_unreachable_open_connector_is_reported(oc):
    _, reply = oc
    reply["value"] = None
    assert "isn't reachable" in run(it.read_sheets("google", "default", "sheet1"))["error"]


def test_calendar_defaults_to_the_coming_week(oc):
    calls, _ = oc
    run(it.list_calendar_events("microsoft", "default"))
    sent = calls[0]["input"]
    assert calls[0]["action"] == "outlook_calendar.list_calendar_view"
    assert sent["startDateTime"] < sent["endDateTime"]
    assert not sent["startDateTime"].startswith("2026-01-01")


def test_outlook_meeting_times_are_converted_to_utc(oc):
    calls, _ = oc
    run(it.create_meeting("microsoft", "default", "Plan", "2026-11-13T10:00:00+02:00",
                          "2026-11-13T11:00:00+02:00", ["a@x.com"]))
    event = calls[0]["input"]["event"]
    assert event["start"] == {"dateTime": "2026-11-13T08:00:00", "timeZone": "UTC"}
    assert event["attendees"] == [{"emailAddress": {"address": "a@x.com"}, "type": "required"}]


def test_google_meeting_without_offset_gets_a_time_zone(oc):
    calls, _ = oc
    run(it.create_meeting("google", "default", "Plan", "2026-11-13T10:00:00", "2026-11-13T11:00:00"))
    event = calls[0]["input"]["event"]
    assert calls[0]["action"] == "googlecalendar.create_event"
    assert event["start"] == {"dateTime": "2026-11-13T10:00:00", "timeZone": "UTC"}


def test_sheets_range_reads_values(oc):
    calls, reply = oc
    reply["value"] = {"ok": True, "data": {"values": [["a"]]}}
    assert run(it.read_sheet_range("google", "default", "S1", "Sheet1!A1:B2")) == {"values": [["a"]]}
    assert calls[0] == {"action": "googlesheets.values_get", "label": "default",
                        "input": {"spreadsheetId": "S1", "range": "Sheet1!A1:B2"}}


def test_google_and_microsoft_are_out_of_the_broker():
    assert "google" not in CONNECTORS and "microsoft" not in CONNECTORS
    assert "google" not in broker.broker_status()["services"]


def test_summary_points_google_and_microsoft_at_open_connector():
    summary = {row["service"]: row for row in it.get_connections_summary()}
    assert summary["google"]["via"] == "OpenConnector"
    assert "Gmail" in summary["google"]["apps"]
    assert "Outlook" in summary["microsoft"]["apps"]
