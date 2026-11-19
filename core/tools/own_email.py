"""Email on the person's own domain (for example you@yourbusiness.co.za),
through its mail server: IMAP to read and SMTP to send. Gmail and Outlook
go through OpenConnector instead (core/tools/integrations.py).

The person types the server details and the account's password into
FreeHand's Connections form. They're kept encrypted with FreeHand's other
connections (core/oauth/manager.py) and sent only to that mail server.
Many providers want an "app password" here rather than the normal one.
"""
from __future__ import annotations

import email
import email.utils
import imaplib
import re
import smtplib
import ssl
from email.header import decode_header, make_header
from email.message import EmailMessage
from typing import Optional

SERVICE = "email"
TIMEOUT = 20


class EmailError(Exception):
    """Plain words for the person."""


def guess_servers(address: str) -> dict:
    """Most small-business hosts use mail.<domain> for both."""
    domain = (address or "").rsplit("@", 1)[-1].strip().lower()
    host = f"mail.{domain}" if domain else ""
    return {"imap_host": host, "imap_port": 993, "smtp_host": host, "smtp_port": 465}


def clean(form: dict) -> dict:
    form = form if isinstance(form, dict) else {}
    address = str(form.get("address") or "").strip()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", address):
        raise EmailError("Give the full email address, for example you@yourbusiness.co.za.")
    password = str(form.get("password") or "")
    if not password:
        raise EmailError("Type the email account's password (or app password).")
    guess = guess_servers(address)
    creds = {"address": address, "username": str(form.get("username") or address).strip(), "password": password}
    for key in ("imap_host", "smtp_host"):
        host = str(form.get(key) or guess[key]).strip().lower()
        if not re.fullmatch(r"[a-z0-9.\-]+\.[a-z]{2,}", host):
            raise EmailError("Give the mail server's name, for example mail.yourbusiness.co.za.")
        creds[key] = host
    for key in ("imap_port", "smtp_port"):
        try:
            creds[key] = int(form.get(key) or guess[key])
        except (TypeError, ValueError):
            raise EmailError("The port must be a number, usually 993 for reading and 465 or 587 for sending.")
    return creds


def _smtp(creds: dict):
    host, port = creds["smtp_host"], int(creds["smtp_port"])
    if port == 465:
        server = smtplib.SMTP_SSL(host, port, timeout=TIMEOUT, context=ssl.create_default_context())
    else:
        server = smtplib.SMTP(host, port, timeout=TIMEOUT)
        server.starttls(context=ssl.create_default_context())
    server.login(creds["username"], creds["password"])
    return server


def _imap(creds: dict):
    box = imaplib.IMAP4_SSL(creds["imap_host"], int(creds["imap_port"]), timeout=TIMEOUT,
                            ssl_context=ssl.create_default_context())
    box.login(creds["username"], creds["password"])
    return box


def _plain(err: Exception, what: str) -> str:
    text = str(err)
    if isinstance(err, (imaplib.IMAP4.error, smtplib.SMTPAuthenticationError)) or "auth" in text.lower():
        return (f"The mail server didn't accept the address and password for {what}. Check the password; "
                "many providers want an app password here, made in the account's security settings.")
    if "getaddrinfo" in text or "Name or service not known" in text or "nodename" in text:
        return f"FreeHand couldn't find the mail server for {what}. Check its name."
    if "timed out" in text.lower():
        return f"The mail server for {what} didn't answer. Check its name and port."
    return f"{what.capitalize()} didn't work: {text[:120]}"


def test(creds: dict) -> dict:
    """Try reading and sending. {"reading": bool, "sending": bool, "problems": [plain words]}."""
    out = {"reading": False, "sending": False, "problems": []}
    try:
        box = _imap(creds)
        box.select("INBOX", readonly=True)
        box.logout()
        out["reading"] = True
    except Exception as e:
        out["problems"].append(_plain(e, "reading email"))
    try:
        server = _smtp(creds)
        server.quit()
        out["sending"] = True
    except Exception as e:
        out["problems"].append(_plain(e, "sending email"))
    return out


def credentials(label: str = "default") -> dict:
    from core.oauth.manager import get_connection
    conn = get_connection(SERVICE, label or "default")
    creds = (conn or {}).get("token_data", {}).get("credentials") if conn else None
    if not creds:
        raise EmailError(f"No email account called '{label}' is connected. Connect one under Connections, Email.")
    return creds


def _header(value) -> str:
    try:
        return str(make_header(decode_header(value or "")))
    except Exception:
        return str(value or "")


def _preview(msg) -> str:
    part = msg
    if msg.is_multipart():
        part = next((p for p in msg.walk() if p.get_content_type() == "text/plain"), None) or \
               next((p for p in msg.walk() if p.get_content_type() == "text/html"), None)
    if part is None:
        return ""
    try:
        text = part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", errors="replace")
    except Exception:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()[:300]


def read(label: str = "default", query: str = "", limit: int = 10) -> dict:
    """The newest emails in the inbox, trimmed like the others (sender,
    subject, date, preview, unread). Reading doesn't mark them as read."""
    _check_whose(label)
    creds = credentials(label)
    try:
        box = _imap(creds)
    except Exception as e:
        raise EmailError(_plain(e, "reading email"))
    try:
        box.select("INBOX", readonly=True)
        criteria = ["TEXT", f'"{query}"'] if query and '"' not in query else ["ALL"]
        _, data = box.search(None, *criteria)
        ids = (data[0] or b"").split()[-limit:]
        _, flagged = box.search(None, "UNSEEN")
        unseen = set((flagged[0] or b"").split())
        emails = []
        for mid in reversed(ids):
            _, parts = box.fetch(mid, "(BODY.PEEK[])")
            raw = next((p[1] for p in parts if isinstance(p, tuple)), b"")
            msg = email.message_from_bytes(raw)
            emails.append({"id": mid.decode(), "from": _header(msg.get("From")), "subject": _header(msg.get("Subject")),
                           "date": msg.get("Date") or "", "unread": mid in unseen, "preview": _preview(msg)})
        return {"emails": emails, "count": len(emails)}
    finally:
        try:
            box.logout()
        except Exception:
            pass


def _check_whose(label: str) -> None:
    """Only a mailbox the current person may use (core/accounts.py)."""
    from core import accounts
    refusal = accounts.refusal(accounts.key("app", "email", label))
    if refusal:
        raise EmailError(refusal)


def send(label: str, to: list, subject: str, body: str) -> dict:
    _check_whose(label)
    creds = credentials(label)
    msg = EmailMessage()
    msg["From"] = creds["address"]
    msg["To"] = ", ".join(to)
    msg["Subject"] = subject
    msg["Date"] = email.utils.formatdate(localtime=True)
    msg["Message-ID"] = email.utils.make_msgid()
    msg.set_content(body)
    try:
        server = _smtp(creds)
    except Exception as e:
        raise EmailError(_plain(e, "sending email"))
    try:
        server.send_message(msg)
    except smtplib.SMTPRecipientsRefused:
        raise EmailError("The mail server refused the address it was sent to. Check it.")
    finally:
        try:
            server.quit()
        except Exception:
            pass
    return {"sent": True, "from": creds["address"], "to": to}
