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
    # Nov 2026: Zoom and Instagram moved too ("if OpenConnector has it, it
    # comes out of the broker").
    ("zoom", "meetings"): "zoom",
    ("instagram", "posts"): "instagram",
}
OC_APP_NAMES = {
    "gmail": "Gmail",
    "googlecalendar": "Google Calendar",
    "googlesheets": "Google Sheets",
    "outlook": "Outlook",
    "outlook_calendar": "Outlook Calendar",
    "zoom": "Zoom",
    "instagram": "Instagram",
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
            url = f"/api/integrations/{app}/authorize?label={urllib.parse.quote(label)}"
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


def _default_window(from_dt: str, to_dt: str) -> tuple:
    """Default calendar window: now to seven days ahead (was all of 2026)."""
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    return from_dt or _iso(now), to_dt or _iso(now + timedelta(days=7))


def _utc_naive(value: str) -> str:
    """'2026-11-13T10:00:00+02:00' -> '2026-11-13T08:00:00' (UTC, no offset).

    Outlook wants a local time plus a time-zone name; FreeHand sends UTC.
    A value without an offset is taken as UTC already.
    """
    from datetime import datetime, timezone
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def _google_time(value: str) -> dict:
    """Google needs an offset or a time zone; a bare time is taken as UTC."""
    from datetime import datetime
    try:
        has_tz = datetime.fromisoformat(value.replace("Z", "+00:00")).tzinfo is not None
    except ValueError:
        has_tz = True
    return {"dateTime": value} if has_tz else {"dateTime": value, "timeZone": "UTC"}


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
        return {"error": "Use IMAP directly for custom email providers"}
    return {"error": f"Email not supported for: {service}"}


async def send_email(service: str = "google", label: str = "default", to: str = "", subject: str = "", body: str = "") -> dict:
    check = _check_write_permission("send_email", service, label)
    if not check.get("allowed"):
        return {"error": "Approval required to send email", "approval": check}
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
    check = _check_write_permission("create_meeting", service, label)
    if not check.get("allowed"):
        return {"error": "Approval required to create meeting", "approval": check}
    if not title or not start or not end:
        return {"error": "title, start, and end are required"}
    people = _recipients(attendees or [])
    if service == "google":
        data, err = await _oc_run(service, "calendar", "create_event", {
            "calendarId": "primary",
            "event": {
                "summary": title,
                "start": _google_time(start),
                "end": _google_time(end),
                "attendees": [{"email": a} for a in people],
            },
        }, label)
        return err or data
    if service == "microsoft":
        data, err = await _oc_run(service, "calendar", "create_event", {
            "event": {
                "subject": title,
                "start": {"dateTime": _utc_naive(start), "timeZone": "UTC"},
                "end": {"dateTime": _utc_naive(end), "timeZone": "UTC"},
                "attendees": [{"emailAddress": {"address": a}, "type": "required"} for a in people],
            },
        }, label)
        return err or data
    return {"error": f"Meeting creation not supported for: {service}"}


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
    check = _check_write_permission("post_to_facebook", service, label)
    if not check.get("allowed"):
        return {"error": "Approval required to post to Facebook", "approval": check}
    if not page_id or not content:
        return {"error": "page_id and content are required"}
    conn, err = await _ensure_token(service, label)
    if err:
        return {"error": err}
    token_data = conn["token_data"]
    url = f"https://graph.facebook.com/v19.0/{page_id}/feed?message={content}&access_token={token_data.get('access_token', '')}"
    return await _call_api(token_data, "POST", url)


async def list_instagram_accounts(service: str = "instagram", label: str = "default") -> dict:
    data, err = await _oc_run("instagram", "posts", "get_current_user", {}, label)
    if err:
        return err
    return [data]


async def post_to_instagram(service: str = "instagram", label: str = "default", content: str = "", media_url: str = "") -> dict:
    check = _check_write_permission("post_to_instagram", service, label)
    if not check.get("allowed"):
        return {"error": "Approval required to post to Instagram", "approval": check}
    if not media_url:
        return {"error": "Instagram posts need a picture: media_url must be a public https image address"}
    data, err = await _oc_run(
        "instagram", "posts", "publish_media",
        {"kind": "image", "imageUrl": media_url, "caption": content}, label,
    )
    return err or data


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
    check = _check_write_permission("schedule_zoom_meeting", service, label)
    if not check.get("allowed"):
        return {"error": "Approval required to schedule Zoom meeting", "approval": check}
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
    } for family in ("google", "microsoft", "zoom", "instagram")]
    for svc in ["facebook", "github", "email"]:
        conns = list_connections(svc)
        for c in conns:
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


