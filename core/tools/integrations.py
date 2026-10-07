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
    if tier == PermissionTier.GOD_MODE:
        return {"allowed": True}
    if action in READ_ONLY:
        return {"allowed": True}
    desc = WRITE_ACTIONS.get(action, ("oauth_access", f"{action} via {service} ({label})"))[1]
    return intercept_action("oauth_access", desc, {"service": service, "label": label})


async def read_email(service: str = "google", label: str = "default", filter_query: str = "") -> dict:
    check = _check_write_permission("read_email", service, label)
    conn, err = await _ensure_token(service, label)
    if err:
        return {"error": err}
    token_data = conn["token_data"]

    if service == "google":
        url = "https://gmail.googleapis.com/gmail/v1/users/me/messages"
        params = {"maxResults": 20}
        if filter_query:
            params["q"] = filter_query
        result = await _call_api(token_data, "GET", url + "?" + "&".join(f"{k}={v}" for k, v in params.items()))
        if "error" in result:
            return result
        msg_ids = [m["id"] for m in result.get("messages", [])]
        threads = []
        for msg_id in msg_ids[:5]:
            msg_url = f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{msg_id}"
            msg = await _call_api(token_data, "GET", msg_url)
            if "error" not in msg:
                headers_resp = msg.get("payload", {}).get("headers", [])
                subject = next((h["value"] for h in headers_resp if h["name"] == "Subject"), "")
                from_addr = next((h["value"] for h in headers_resp if h["name"] == "From"), "")
                threads.append({"subject": subject, "from": from_addr, "id": msg_id})
        return {"threads": threads, "total": len(msg_ids)}

    elif service == "microsoft":
        url = "https://graph.microsoft.com/v1.0/me/messages?$top=10"
        if filter_query:
            url += f"&$filter={filter_query}"
        return await _call_api(token_data, "GET", url)

    elif service == "email":
        return {"error": "Use IMAP directly for custom email providers"}

    return {"error": f"Email not supported for: {service}"}


async def send_email(service: str = "google", label: str = "default", to: str = "", subject: str = "", body: str = "") -> dict:
    check = _check_write_permission("send_email", service, label)
    if not check.get("allowed"):
        return {"error": "Approval required to send email", "approval": check}
    if not to or not subject or not body:
        return {"error": "to, subject, and body are required"}
    conn, err = await _ensure_token(service, label)
    if err:
        return {"error": err}
    token_data = conn["token_data"]

    if service == "google":
        import base64
        raw = f"From: sender@example.com\nTo: {to}\nSubject: {subject}\n\n{body}"
        encoded = base64.urlsafe_b64encode(raw.encode()).decode()
        payload = {"raw": encoded}
        url = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
        return await _call_api(token_data, "POST", url, body=payload)

    elif service == "microsoft":
        payload = {
            "message": {
                "subject": subject,
                "body": {"contentType": "Text", "content": body},
                "toRecipients": [{"emailAddress": {"address": to}}],
            }
        }
        url = "https://graph.microsoft.com/v1.0/me/mailFolders/outbox/messages"
        return await _call_api(token_data, "POST", url, body=payload)

    return {"error": f"Send email not supported for: {service}"}


async def list_calendar_events(service: str = "google", label: str = "default", from_dt: str = "", to_dt: str = "") -> dict:
    _check_write_permission("list_calendar_events", service, label)
    conn, err = await _ensure_token(service, label)
    if err:
        return {"error": err}
    token_data = conn["token_data"]

    if service == "google":
        start = from_dt or "2026-01-01T00:00:00Z"
        end = to_dt or "2026-12-31T23:59:59Z"
        url = f"https://www.googleapis.com/calendar/v3/calendars/primary/events?timeMin={start}&timeMax={end}&maxResults=20"
        return await _call_api(token_data, "GET", url)

    elif service == "microsoft":
        url = f"https://graph.microsoft.com/v1.0/me/calendarview?startdatetime={from_dt or '2026-01-01'}&enddatetime={to_dt or '2026-12-31'}&$select=subject,start,end,location"
        return await _call_api(token_data, "GET", url)

    return {"error": f"Calendar not supported for: {service}"}


async def create_meeting(service: str = "google", label: str = "default", title: str = "", start: str = "", end: str = "", attendees: list = None) -> dict:
    check = _check_write_permission("create_meeting", service, label)
    if not check.get("allowed"):
        return {"error": "Approval required to create meeting", "approval": check}
    conn, err = await _ensure_token(service, label)
    if err:
        return {"error": err}
    token_data = conn["token_data"]

    if service == "google":
        attendees_list = [{"email": a} for a in (attendees or [])]
        payload = {
            "summary": title,
            "start": {"dateTime": start},
            "end": {"dateTime": end},
            "attendees": attendees_list,
        }
        url = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
        return await _call_api(token_data, "POST", url, body=payload)

    elif service == "microsoft":
        attendees_list = [{"emailAddress": {"address": a}} for a in (attendees or [])]
        payload = {
            "subject": title,
            "start": {"dateTime": start, "timeZone": "UTC"},
            "end": {"dateTime": end, "timeZone": "UTC"},
            "attendees": attendees_list,
        }
        url = "https://graph.microsoft.com/v1.0/me/calendar/events"
        return await _call_api(token_data, "POST", url, body=payload)

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
    _check_write_permission("list_instagram_accounts", service, label)
    conn, err = await _ensure_token(service, label)
    if err:
        return {"error": err}
    token_data = conn["token_data"]
    url = f"https://graph.facebook.com/v19.0/me?fields=id,username,account_type&access_token={token_data.get('access_token', '')}"
    result = await _call_api(token_data, "GET", url)
    if "error" in result:
        return result
    return [result]


async def post_to_instagram(service: str = "instagram", label: str = "default", content: str = "", media_url: str = "") -> dict:
    check = _check_write_permission("post_to_instagram", service, label)
    if not check.get("allowed"):
        return {"error": "Approval required to post to Instagram", "approval": check}
    if not content and not media_url:
        return {"error": "content or media_url are required"}
    conn, err = await _ensure_token(service, label)
    if err:
        return {"error": err}
    token_data = conn["token_data"]
    if media_url:
        container_id = None
        create_url = f"https://graph.facebook.com/v19.0/me/media?image_url={media_url}&caption={content}&access_token={token_data.get('access_token', '')}"
        create_resp = await _call_api(token_data, "POST", create_url)
        if "error" in create_resp:
            return create_resp
        container_id = create_resp.get("id")
        publish_url = f"https://graph.facebook.com/v19.0/me/media_publish?creation_id={container_id}&access_token={token_data.get('access_token', '')}"
        return await _call_api(token_data, "POST", publish_url)
    return {"error": "Instagram requires media_url for posting"}


async def list_zoom_meetings(service: str = "zoom", label: str = "default", page_size: int = 30) -> dict:
    _check_write_permission("list_zoom_meetings", service, label)
    conn, err = await _ensure_token(service, label)
    if err:
        return {"error": err}
    token_data = conn["token_data"]
    url = f"https://api.zoom.us/v2/users/me/meetings?page_size={page_size}"
    result = await _call_api(token_data, "GET", url)
    if "error" in result:
        return result
    meetings = result.get("meetings", [])
    return [{"id": m.get("id"), "topic": m.get("topic"), "start_time": m.get("start_time"), "status": m.get("status")} for m in meetings]


async def schedule_zoom_meeting(service: str = "zoom", label: str = "default", title: str = "", start_time: str = "", duration: int = 60) -> dict:
    check = _check_write_permission("schedule_zoom_meeting", service, label)
    if not check.get("allowed"):
        return {"error": "Approval required to schedule Zoom meeting", "approval": check}
    if not title or not start_time:
        return {"error": "title and start_time are required"}
    conn, err = await _ensure_token(service, label)
    if err:
        return {"error": err}
    token_data = conn["token_data"]
    payload = {
        "topic": title,
        "type": 2,
        "start_time": start_time,
        "duration": duration,
        "settings": {"host_video": True, "participant_video": True},
    }
    url = "https://api.zoom.us/v2/users/me/meetings"
    return await _call_api(token_data, "POST", url, body=payload)


def get_connections_summary() -> List[Dict]:
    results = []
    for svc in ["google", "microsoft", "zoom", "facebook", "instagram", "github", "email"]:
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
    _check_write_permission("read_sheets", service, label)
    if not spreadsheet_id:
        return {"error": "spreadsheet_id is required"}
    conn, err = await _ensure_token(service, label)
    if err:
        return {"error": err}
    token_data = conn["token_data"]
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}"
    result = await _call_api(token_data, "GET", url)
    if "error" in result:
        return result
    sheets = result.get("sheets", [])
    ranges = [s.get("properties", {}).get("title", "Sheet") for s in sheets]
    return {"spreadsheet_id": spreadsheet_id, "title": result.get("properties", {}).get("title", ""), "sheets": ranges}


async def read_sheet_range(service: str = "google", label: str = "default", spreadsheet_id: str = "", range_str: str = "") -> dict:
    _check_write_permission("read_sheet_range", service, label)
    if not spreadsheet_id or not range_str:
        return {"error": "spreadsheet_id and range_str (e.g. Sheet1!A1:D10) are required"}
    conn, err = await _ensure_token(service, label)
    if err:
        return {"error": err}
    token_data = conn["token_data"]
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}/values/{range_str}"
    return await _call_api(token_data, "GET", url)


async def list_zoom_recordings(service: str = "zoom", label: str = "default") -> dict:
    _check_write_permission("list_zoom_recordings", service, label)
    conn, err = await _ensure_token(service, label)
    if err:
        return {"error": err}
    token_data = conn["token_data"]
    url = "https://api.zoom.us/v2/users/me/meetings?page_size=10"
    result = await _call_api(token_data, "GET", url)
    if "error" in result:
        return result
    meetings = result.get("meetings", [])
    recordings = []
    for m in meetings:
        mid = m.get("id", "")
        rec_url = f"https://api.zoom.us/v2/meetings/{mid}/recordings"
        rec = await _call_api(token_data, "GET", rec_url)
        if "error" not in rec and rec.get("recordings"):
            for r in rec["recordings"]:
                recordings.append({"meeting_id": mid, "topic": m.get("topic"), "recording_url": r.get("playback_url", ""), "type": r.get("type")})
    return recordings
