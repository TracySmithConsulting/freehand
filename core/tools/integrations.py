import json
from typing import Dict, List, Optional

import aiohttp

from core.oauth.manager import get_connection, list_connections, refresh_token, is_token_expired
from core.oauth.providers import get_connector
from core.security import intercept_action, get_current_tier, PermissionTier

READ_ONLY = frozenset({
    "read_email", "list_calendar_events", "list_facebook_pages",
    "list_instagram_accounts", "list_zoom_meetings",
    "get_email_folders",
})

WRITE_ACTIONS = {
    "send_email": ("oauth_access", "Send email via {service} ({label})"),
    "create_meeting": ("oauth_access", "Create meeting via {service} ({label})"),
    "post_to_facebook": ("oauth_access", "Post to Facebook page via {service} ({label})"),
    "post_to_instagram": ("oauth_access", "Post to Instagram via {service} ({label})"),
    "schedule_zoom_meeting": ("oauth_access", "Schedule Zoom meeting via {service} ({label})"),
}


async def _ensure_token(service, label):
    from core import accounts
    refusal = accounts.refusal(accounts.key("app", service, label))
    if refusal:
        return None, refusal
    conn = get_connection(service, label)
    if not conn:
        return None, f"Not connected: {service} ({label})"
    if is_token_expired(service, label):
        refreshed = await refresh_token(service, label)
        if refreshed:
            conn = get_connection(service, label)
        else:
            return None, f"Token expired for {service} ({label}) — reconnect required"
    return conn, None


async def _call_api(token_data: dict, method: str, url: str, headers: dict = None, body: dict = None) -> dict:
    auth_headers = {"Authorization": f"Bearer {token_data.get('access_token', '')}"}
    if headers:
        auth_headers.update(headers)

    timeout = aiohttp.ClientTimeout(total=30)
    try:
        async with aiohttp.ClientSession() as session:
            if method.upper() == "GET":
                async with session.get(url, headers=auth_headers, timeout=timeout) as resp:
                    if resp.status >= 400:
                        text = await resp.text()
                        return {"error": f"HTTP {resp.status}: {text}"}
                    return await resp.json()
            elif method.upper() == "POST":
                async with session.post(url, headers=auth_headers, json=body, timeout=timeout) as resp:
                    if resp.status >= 400:
                        text = await resp.text()
                        return {"error": f"HTTP {resp.status}: {text}"}
                    try:
                        return await resp.json()
                    except Exception:
                        return {"status": resp.status, "text": await resp.text()}
            elif method.upper() == "PUT":
                async with session.put(url, headers=auth_headers, json=body, timeout=timeout) as resp:
                    if resp.status >= 400:
                        text = await resp.text()
                        return {"error": f"HTTP {resp.status}: {text}"}
                    try:
                        return await resp.json()
                    except Exception:
                        return {"status": resp.status}
    except aiohttp.ClientError as e:
        return {"error": f"Network error: {e}"}
    except Exception as e:
        return {"error": str(e)}
    return {"error": "Unknown error"}


def _check_write_permission(action: str, service: str, label: str) -> dict:
    tier = get_current_tier()
    if tier == PermissionTier.FULL_ACCESS:
        return {"allowed": True}
    if action in READ_ONLY:
        return {"allowed": True}
    desc = WRITE_ACTIONS.get(action, ("oauth_access", f"{action} via {service} ({label})"))[1]
    return intercept_action("oauth_access", desc, {"service": service, "label": label})


# ── Google and Microsoft run through OpenConnector ─────────────────────
#
# FreeHand used to call Gmail, Google Calendar/Sheets, Outlook and
# Microsoft Graph itself, with tokens from its own OAuth broker. Those
# accounts now live in OpenConnector (OC), which holds the tokens, renews
# them, and runs the calls. FreeHand keeps its tool names and the
# `service` ("google" / "microsoft") and `label` arguments; the label is the
# connection name chosen in OpenConnector ("default" unless renamed).

OC_APPS = {
    ("google", "email"): "gmail",
    ("google", "calendar"): "googlecalendar",
    ("google", "sheets"): "googlesheets",
    ("microsoft", "email"): "outlook",
    ("microsoft", "calendar"): "outlook_calendar",
    # Nov 2026: Zoom moved too ("if OpenConnector has it, it comes out of
    # the broker"). Instagram stays on Composio: see _composio_run.
    ("zoom", "meetings"): "zoom",
}
OC_APP_NAMES = {
    "gmail": "Gmail",
    "googlecalendar": "Google Calendar",
    "googlesheets": "Google Sheets",
    "outlook": "Outlook",
    "outlook_calendar": "Outlook Calendar",
    "zoom": "Zoom",
}
OC_SERVICES = frozenset(service for service, _ in OC_APPS)


def _oc_console_url() -> str:
    import os
    return os.environ.get("OOMOL_CONNECT_CONSOLE_URL", "").strip().rstrip("/") or "http://localhost:3001"


async def _oc_run(service: str, kind: str, action: str, inputs: dict, label: str):
    """Run one OpenConnector action. Returns (data, None) or (None, error dict)."""
    import asyncio
    from core.oauth import open_connector

    app = OC_APPS.get((service, kind))
    if not app:
        return None, {"error": f"{kind.capitalize()} is not supported for: {service}"}
    name = OC_APP_NAMES[app]
    label = label or "default"
    inputs = {k: v for k, v in inputs.items() if v not in (None, "", [])}
    result = await asyncio.to_thread(
        open_connector.execute_action, f"{app}.{action}", inputs, label, 60.0
    )
    if result is None:
        return None, {"error": "OpenConnector isn't reachable. Check that it is running."}
    if not result.get("ok"):
        err = result.get("error") or {}
        if err.get("code") == "connection_not_found":
            import urllib.parse
            url = "/#connections"  # the Connect button there signs in safely
            return None, {
                "error": (
                    f"No {name} account called '{label}' is connected yet. In FreeHand, open "
                    f"Connections, choose Connect {name} and name the account '{label}'."
                ),
                "connect_url": url,
            }
        return None, {"error": f"{name}: {err.get('message') or err.get('code') or 'request failed'}"}
    return result.get("data") or {}, None


def _recipients(value) -> list:
    if isinstance(value, (list, tuple)):
        items = value
    else:
        items = str(value or "").replace(";", ",").split(",")
    return [str(x).strip() for x in items if str(x).strip()]


def _iso(dt) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _local(value: str):
    """A date and time from the model, as a time-zone-aware datetime. One
    without a time zone is the person's own time (it used to be taken as
    UTC, so "10:00" landed at 12:00 in South Africa). None if unreadable."""
    from datetime import datetime
    from core import scheduler
    try:
        dt = datetime.fromisoformat(str(value or "").strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=scheduler.local_tz())


def _default_window(from_dt: str, to_dt: str) -> tuple:
    """Default calendar window: now to seven days ahead (was all of 2026).
    Times without a time zone are the person's own."""
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    start, end = _local(from_dt) if from_dt else None, _local(to_dt) if to_dt else None
    return (_iso(start.astimezone(timezone.utc)) if start else from_dt or _iso(now),
            _iso(end.astimezone(timezone.utc)) if end else to_dt or _iso(now + timedelta(days=7)))


def _utc_naive(value: str) -> str:
    """'2026-11-13T10:00:00+02:00' -> '2026-11-13T08:00:00' (UTC, no offset).

    Outlook wants a local time plus a time-zone name; FreeHand sends UTC.
    A value without an offset is the person's own time.
    """
    from datetime import timezone
    dt = _local(value)
    if dt is None:
        return value
    return dt.astimezone(timezone.utc).replace(tzinfo=None).strftime("%Y-%m-%dT%H:%M:%S")


def _google_time(value: str) -> dict:
    """Google needs an offset or a time zone; a bare time is the person's own."""
    dt = _local(value)
    if dt is None:
        return {"dateTime": value}
    return {"dateTime": dt.isoformat()}


def _when(value: dict) -> str:
    """An event's start or end, as the person's own time where it can be."""
    from datetime import datetime
    from core import scheduler
    value = value if isinstance(value, dict) else {}
    if value.get("date"):
        return value["date"]  # all day
    raw = str(value.get("dateTime") or "")
    if not raw:
        return ""
    import re
    # Outlook sends seven decimal places ("08:00:00.0000000"); Python reads at most six.
    cleaned = re.sub(r"\.(\d{1,6})\d*", r".\1", raw.replace("Z", "+00:00"))
    try:
        dt = datetime.fromisoformat(cleaned)
    except ValueError:
        return raw
    if dt.tzinfo is None:  # Outlook gives UTC (or the zone named beside it)
        from datetime import timezone
        zone = str(value.get("timeZone") or "UTC")
        try:
            from zoneinfo import ZoneInfo
            dt = dt.replace(tzinfo=timezone.utc if zone.upper() == "UTC" else ZoneInfo(zone))
        except Exception:
            dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(scheduler.local_tz()).strftime("%Y-%m-%d %H:%M")


def slim_event(service: str, e: dict) -> dict:
    if service == "microsoft":
        who = [((a.get("emailAddress") or {}).get("address") or "") for a in e.get("attendees") or []]
        out = {"id": e.get("id"), "title": e.get("subject") or "", "start": _when(e.get("start")),
               "end": _when(e.get("end")), "all_day": bool(e.get("isAllDay")),
               "where": (e.get("location") or {}).get("displayName") or "",
               "notes": (e.get("bodyPreview") or "")[:200]}
    else:
        who = [a.get("email") or "" for a in e.get("attendees") or [] if not a.get("self")]
        out = {"id": e.get("id"), "title": e.get("summary") or "(no title)", "start": _when(e.get("start")),
               "end": _when(e.get("end")), "all_day": bool((e.get("start") or {}).get("date")),
               "where": e.get("location") or ("Google Meet" if e.get("hangoutLink") else ""),
               "notes": (e.get("description") or "")[:200]}
    who = [w for w in who if w][:10]
    if who:
        out["with"] = who
    return {k: v for k, v in out.items() if v not in ("", None, False)}  # all_day only when it is


def slim_events(service: str, data) -> dict:
    """What the model needs from a calendar: title, when (in the person's
    own time), where, who and a line of notes. Google sent about 500 tokens
    per event (links, reminders, conference details)."""
    if not isinstance(data, dict) or data.get("error"):
        return data
    items = data.get("items") if service == "google" else (data.get("value") or data.get("events"))
    events = [slim_event(service, e) for e in (items or data.get("events") or []) if isinstance(e, dict)]
    return {"events": events, "count": len(events), "times_are": "the person's own time"}


def slim_emails(service: str, data) -> dict:
    """What the model needs from a list of emails: who, subject, when, a
    short preview, and whether it's been read. Outlook sends each email's
    whole body and dozens of other fields: about 132,500 tokens for 10
    emails (Nov 2026), so the model saw one or two before the cut-off."""
    if not isinstance(data, dict) or data.get("error") or "emails" in data:
        return data  # an error, or already trimmed (own-domain email)
    out = []
    for m in data.get("messages") or []:
        if not isinstance(m, dict):
            continue
        if service == "microsoft":
            addr = (m.get("from") or {}).get("emailAddress") or {}
            sender = f"{addr.get('name') or ''} <{addr.get('address') or ''}>".strip()
            out.append({"id": m.get("id"), "from": sender, "subject": m.get("subject") or "",
                        "date": m.get("receivedDateTime") or "", "unread": not m.get("isRead", True),
                        "preview": (m.get("bodyPreview") or "")[:300],
                        "attachments": bool(m.get("hasAttachments"))})
        else:
            labels = m.get("labelIds") or []
            out.append({"id": m.get("messageId"), "from": m.get("sender") or "", "subject": m.get("subject") or "",
                        "date": m.get("messageTimestamp") or "", "unread": "UNREAD" in labels,
                        "preview": (m.get("snippet") or "")[:300]})
    return {"emails": out, "count": len(out)}


async def read_email(service: str = "google", label: str = "default", filter_query: str = "") -> dict:
    if service == "google":
        data, err = await _oc_run(service, "email", "fetch_emails", {
            "query": filter_query, "maxResults": 10, "detail": "summary",
        }, label)
        return err or data
    if service == "microsoft":
        data, err = await _oc_run(service, "email", "list_messages", {
            "top": 10, "filter": filter_query,
        }, label)
        return err or data
    if service == "email":
        import asyncio
        from core.tools import own_email
        try:
            return await asyncio.to_thread(own_email.read, label, filter_query)
        except own_email.EmailError as e:
            return {"error": str(e)}
    return {"error": f"Email not supported for: {service}"}


async def send_email(service: str = "google", label: str = "default", to: str = "", subject: str = "", body: str = "") -> dict:
    # The approval is asked for once, before this runs (core/agent.py and
    # core/mcp_server.py); an approved send then runs straight through.
    recipients = _recipients(to)
    if not recipients or not subject or not body:
        return {"error": "to, subject, and body are required"}
    if service == "google":
        data, err = await _oc_run(service, "email", "send_email", {
            "to": recipients[0], "extraRecipients": recipients[1:], "subject": subject, "body": body,
        }, label)
        return err or {"sent": True, **data}
    if service == "microsoft":
        # The old code saved Outlook mail to the Outbox folder, which never sends it.
        data, err = await _oc_run(service, "email", "send_email", {
            "toRecipients": recipients, "subject": subject, "body": body,
        }, label)
        return err or {"sent": True, **data}
    if service == "email":
        import asyncio
        from core.tools import own_email
        try:
            return await asyncio.to_thread(own_email.send, label, recipients, subject, body)
        except own_email.EmailError as e:
            return {"error": str(e)}
    return {"error": f"Send email not supported for: {service}"}


async def list_calendar_events(service: str = "google", label: str = "default", from_dt: str = "", to_dt: str = "") -> dict:
    start, end = _default_window(from_dt, to_dt)
    if service == "google":
        data, err = await _oc_run(service, "calendar", "list_events", {
            "calendarId": "primary", "timeMin": start, "timeMax": end,
            "singleEvents": True, "orderBy": "startTime", "maxResults": 20,
        }, label)
        return err or data
    if service == "microsoft":
        data, err = await _oc_run(service, "calendar", "list_calendar_view", {
            "startDateTime": start, "endDateTime": end, "top": 20,
        }, label)
        return err or data
    return {"error": f"Calendar not supported for: {service}"}


async def create_meeting(service: str = "google", label: str = "default", title: str = "", start: str = "", end: str = "", attendees: list = None) -> dict:
    # Not one of the agent's tools, so nothing asks before it runs: it asks itself.
    check = _check_write_permission("create_meeting", service, label)
    if not check.get("allowed"):
        return {"error": "Approval required to create meeting", "approval": check}
    return await create_calendar_event(service, label, title, start, end, attendees)


async def create_calendar_event(service: str = "google", label: str = "default", title: str = "",
                                start: str = "", end: str = "", attendees=None, location: str = "",
                                description: str = "", duration_minutes=None) -> dict:
    """Add an event to the person's calendar. The approval is asked once,
    before this runs (core/agent.py, core/mcp_server.py). Invitations aren't
    emailed: people are added to the event only."""
    from datetime import timedelta
    title = str(title or "").strip()
    begins = _local(start)
    if not title or begins is None:
        return {"error": "Give the event a name and a start, for example 2026-11-20T10:00."}
    ends = _local(end) if end else None
    if ends is None:
        try:
            minutes = int(duration_minutes or 60)
        except (TypeError, ValueError):
            minutes = 60
        ends = begins + timedelta(minutes=max(5, min(minutes, 24 * 60)))
    if ends <= begins:
        return {"error": "The event has to end after it starts."}
    people = _recipients(attendees or [])
    if service == "google":
        event = {"summary": title, "start": {"dateTime": begins.isoformat()}, "end": {"dateTime": ends.isoformat()}}
        if people:
            event["attendees"] = [{"email": a} for a in people]
        if location:
            event["location"] = str(location)[:300]
        if description:
            event["description"] = str(description)[:3000]
        data, err = await _oc_run(service, "calendar", "create_event", {"calendarId": "primary", "event": event}, label)
        return err or {"added": True, **slim_event("google", data or {})}
    if service == "microsoft":
        event = {
            "subject": title,
            "start": {"dateTime": _utc_naive(begins.isoformat()), "timeZone": "UTC"},
            "end": {"dateTime": _utc_naive(ends.isoformat()), "timeZone": "UTC"},
        }
        if people:
            event["attendees"] = [{"emailAddress": {"address": a}, "type": "required"} for a in people]
        if location:
            event["location"] = {"displayName": str(location)[:300]}
        if description:
            event["body"] = {"contentType": "text", "content": str(description)[:3000]}
        data, err = await _oc_run(service, "calendar", "create_event", {"event": event}, label)
        return err or {"added": True, **slim_event("microsoft", data or {})}
    return {"error": f"Adding events isn't possible for: {service}"}


async def list_facebook_pages(service: str = "facebook", label: str = "default") -> dict:
    _check_write_permission("list_facebook_pages", service, label)
    conn, err = await _ensure_token(service, label)
    if err:
        return {"error": err}
    token_data = conn["token_data"]
    url = "https://graph.facebook.com/v19.0/me/accounts?fields=id,name,access_token&access_token={}".format(
        token_data.get("access_token", "")
    )
    result = await _call_api(token_data, "GET", url)
    if "error" in result:
        return result
    return result.get("data", [])


async def post_to_facebook(service: str = "facebook", label: str = "default", page_id: str = "", content: str = "") -> dict:
    # One approval, asked before this runs (core/agent.py, core/mcp_server.py).
    if not page_id or not content:
        return {"error": "page_id and content are required"}
    conn, err = await _ensure_token(service, label)
    if err:
        return {"error": err}
    token_data = conn["token_data"]
    url = f"https://graph.facebook.com/v19.0/{page_id}/feed?message={content}&access_token={token_data.get('access_token', '')}"
    return await _call_api(token_data, "POST", url)


async def _composio_run(service: str, slug: str, args: dict, label: str):
    """Run one Composio action on the connected account.

    Returns (data, None) or (None, error dict). Instagram goes through
    Composio: OpenConnector would need the user's own Meta developer app,
    which won't accept a home computer's http://localhost address."""
    from core.oauth import composio

    label = label or "default"
    name = service.capitalize()
    from core import accounts
    refusal = accounts.refusal(accounts.key("composio", service, label))
    if refusal:
        return None, {"error": refusal}
    account = composio.get_account(service, label)
    if not account or not account.get("connected_account_id"):
        import urllib.parse
        return None, {
            "error": (f"No {name} account called '{label}' is connected yet. In FreeHand, open "
                      f"Connections, choose Connect {name} and name the account '{label}'."),
            "connect_url": "/#connections",
        }
    args = {k: v for k, v in (args or {}).items() if v not in (None, "", [])}
    try:
        response = await composio.execute_tool(slug, account["connected_account_id"], args,
                                               user_id=account.get("user_id", ""))
    except composio.ComposioError as exc:
        return None, {"error": f"{name}: {exc}"}
    if response.get("successful") is False or response.get("error"):
        return None, {"error": f"{name}: {response.get('error') or 'request failed'}"}
    return response.get("data", response) or {}, None


def _first_id(data) -> str:
    """The id in a Composio/Graph reply ({"id"} or {"data": {"id"}})."""
    if isinstance(data, dict):
        if data.get("id"):
            return str(data["id"])
        for key in ("data", "response_data", "result"):
            if isinstance(data.get(key), dict) and data[key].get("id"):
                return str(data[key]["id"])
    return ""


async def list_instagram_accounts(service: str = "instagram", label: str = "default") -> dict:
    data, err = await _composio_run("instagram", "INSTAGRAM_GET_USER_INFO", {}, label)
    if err:
        return err
    return [data]


async def post_to_instagram(service: str = "instagram", label: str = "default", content: str = "", media_url: str = "") -> dict:
    # One approval, asked before this runs (core/agent.py, core/mcp_server.py).
    if not media_url:
        return {"error": "Instagram posts need a picture: media_url must be a public https image address"}
    # Instagram publishes in two steps: upload the picture as a "container",
    # then publish that container.
    user, err = await _composio_run("instagram", "INSTAGRAM_GET_USER_INFO", {}, label)
    if err:
        return err
    ig_user_id = _first_id(user)
    container, err = await _composio_run("instagram", "INSTAGRAM_CREATE_MEDIA_CONTAINER", {
        "ig_user_id": ig_user_id, "image_url": media_url, "caption": content,
    }, label)
    if err:
        return err
    creation_id = _first_id(container)
    if not creation_id:
        return {"error": "Instagram didn't accept the picture. Check the image address is public and ends in .jpg."}
    published, err = await _composio_run("instagram", "INSTAGRAM_POST_IG_USER_MEDIA_PUBLISH", {
        "ig_user_id": ig_user_id, "creation_id": creation_id,
    }, label)
    return err or published


async def list_zoom_meetings(service: str = "zoom", label: str = "default", page_size: int = 30) -> dict:
    data, err = await _oc_run(
        "zoom", "meetings", "list_meetings",
        {"userId": "me", "pageSize": max(1, min(int(page_size or 30), 300))}, label,
    )
    if err:
        return err
    meetings = data.get("meetings", []) if isinstance(data, dict) else []
    return [{"id": m.get("id"), "topic": m.get("topic"), "start_time": m.get("start_time"), "status": m.get("status")} for m in meetings]


async def schedule_zoom_meeting(service: str = "zoom", label: str = "default", title: str = "", start_time: str = "", duration: int = 60) -> dict:
    # One approval, asked before this runs (core/agent.py, core/mcp_server.py).
    if not title or not start_time:
        return {"error": "title and start_time are required"}
    data, err = await _oc_run(
        "zoom", "meetings", "create_meeting",
        {"userId": "me", "topic": title, "type": 2, "startTime": start_time,
         "duration": max(1, min(int(duration or 60), 1440))},
        label,
    )
    return err or data


def get_connections_summary() -> List[Dict]:
    results = [{
        "service": family,
        "via": "OpenConnector",
        "apps": [OC_APP_NAMES[app] for (svc, _), app in OC_APPS.items() if svc == family],
        "note": (
            "Connected in OpenConnector. Use the connection name chosen there as the "
            f"label ('default' unless renamed). Manage accounts at {_oc_console_url()}."
        ),
    } for family in ("google", "microsoft", "zoom")]
    from core import accounts
    for svc in ["facebook", "github", "email"]:
        conns = list_connections(svc)
        for c in conns:
            if not accounts.may_use(accounts.key("app", svc, c["label"])):
                continue  # someone else's: not even its name
            results.append({
                "service": svc,
                "label": c["label"],
                "status": c["status"],
                "scopes": c["scopes"].split(",") if c["scopes"] else [],
                "connected_at": c["connected_at"],
            })
    return results


async def read_sheets(service: str = "google", label: str = "default", spreadsheet_id: str = "") -> dict:
    if not spreadsheet_id:
        return {"error": "spreadsheet_id is required"}
    if service != "google":
        return {"error": f"Sheets not supported for: {service}"}
    data, err = await _oc_run(service, "sheets", "get_spreadsheet_info", {"spreadsheetId": spreadsheet_id}, label)
    if err:
        return err
    sheets = data.get("sheets", [])
    return {
        "spreadsheet_id": spreadsheet_id,
        "title": data.get("properties", {}).get("title", ""),
        "sheets": [sh.get("properties", {}).get("title", "Sheet") for sh in sheets],
    }


async def read_sheet_range(service: str = "google", label: str = "default", spreadsheet_id: str = "", range_str: str = "") -> dict:
    if not spreadsheet_id or not range_str:
        return {"error": "spreadsheet_id and range_str (e.g. Sheet1!A1:D10) are required"}
    if service != "google":
        return {"error": f"Sheets not supported for: {service}"}
    data, err = await _oc_run(service, "sheets", "values_get", {"spreadsheetId": spreadsheet_id, "range": range_str}, label)
    return err or data


