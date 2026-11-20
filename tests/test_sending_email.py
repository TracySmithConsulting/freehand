"""Sending email, and email on the person's own domain (Nov 2026).

- send_email is a real tool now: Gmail, Outlook, or the person's own
  domain. It's asked for once, with who it's to and what it says.
- Approving a held post or email now really does it: an inner check used
  to ask again, so nothing was ever posted at Semi-autonomous.
- "Connect email" (own domain) is a form; it used to open an empty address.
"""
import asyncio
import email
import json

import pytest

from core import agent, security
from core.tools import integrations, own_email


# ── Pretend mail servers ──────────────────────────────────────────────

class FakeSMTP:
    sent = []
    fail_login = False

    def __init__(self, host, port, timeout=None, context=None):
        self.host, self.port = host, port

    def starttls(self, context=None):
        pass

    def login(self, user, password):
        if FakeSMTP.fail_login:
            import smtplib
            raise smtplib.SMTPAuthenticationError(535, b"bad")

    def send_message(self, msg):
        FakeSMTP.sent.append(msg)

    def quit(self):
        pass


RAW = (b"From: Sam <sam@x.com>\r\nSubject: =?utf-8?q?Invoice_due?=\r\nDate: Sun, 15 Nov 2026 08:00:00 +0200\r\n"
       b"Content-Type: text/plain; charset=utf-8\r\n\r\nPlease pay by Friday. Thanks!\r\n")


class FakeIMAP:
    def __init__(self, host, port, timeout=None, ssl_context=None):
        pass

    def login(self, user, password):
        pass

    def select(self, box, readonly=False):
        return "OK", [b"1"]

    def search(self, charset, *criteria):
        # Two emails in the inbox; the newest (7) is unread.
        return "OK", [b"7" if criteria == ("UNSEEN",) else b"6 7"]

    def fetch(self, mid, what):
        return "OK", [(b"1 (BODY[] {100}", RAW)]

    def logout(self):
        pass


@pytest.fixture
def servers(monkeypatch):
    FakeSMTP.sent, FakeSMTP.fail_login = [], False
    monkeypatch.setattr(own_email.smtplib, "SMTP_SSL", FakeSMTP)
    monkeypatch.setattr(own_email.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(own_email.imaplib, "IMAP4_SSL", FakeIMAP)
    return FakeSMTP


@pytest.fixture
def connected(servers):
    from core.oauth.manager import save_connection
    creds = own_email.clean({"address": "tracy@mybusiness.co.za", "password": "app-password"})
    save_connection("email", "work", {"credentials": creds, "expires_in": None}, ["imap", "smtp"])
    return creds


def test_server_names_are_guessed_from_the_address():
    assert own_email.guess_servers("tracy@mybusiness.co.za") == {
        "imap_host": "mail.mybusiness.co.za", "imap_port": 993, "smtp_host": "mail.mybusiness.co.za", "smtp_port": 465}


@pytest.mark.parametrize("form, words", [
    ({"address": "not an address", "password": "x"}, "full email address"),
    ({"address": "a@b.co.za"}, "password"),
    ({"address": "a@b.co.za", "password": "x", "imap_host": "not a host!"}, "mail server's name"),
    ({"address": "a@b.co.za", "password": "x", "smtp_port": "lots"}, "port must be a number"),
])
def test_form_mistakes_get_plain_answers(form, words):
    with pytest.raises(own_email.EmailError, match=words):
        own_email.clean(form)


def test_reading_own_domain_email(connected):
    out = asyncio.run(integrations.read_email("email", "work", ""))
    assert out["count"] == 2
    first = out["emails"][0]
    assert first == {"id": "7", "from": "Sam <sam@x.com>", "subject": "Invoice due",
                     "date": "Sun, 15 Nov 2026 08:00:00 +0200", "unread": True,
                     "preview": "Please pay by Friday. Thanks!"}


def test_sending_from_own_domain(connected, servers):
    out = asyncio.run(integrations.send_email("email", "work", "sam@x.com, lee@y.com", "Hello", "Hi there"))
    assert out == {"sent": True, "from": "tracy@mybusiness.co.za", "to": ["sam@x.com", "lee@y.com"]}
    msg = servers.sent[0]
    assert msg["From"] == "tracy@mybusiness.co.za" and msg["To"] == "sam@x.com, lee@y.com"
    assert msg.get_content().strip() == "Hi there"


def test_a_wrong_password_is_said_plainly(connected, servers):
    servers.fail_login = True
    out = asyncio.run(integrations.send_email("email", "work", "sam@x.com", "Hello", "Hi"))
    assert "app password" in out["error"]


def test_not_connected_says_where_to_connect():
    out = asyncio.run(integrations.read_email("email", "nobody", ""))
    assert "Connect one under Connections, Email" in out["error"]


# ── The form ──────────────────────────────────────────────────────────

@pytest.fixture
def client(monkeypatch):
    from fastapi.testclient import TestClient
    import server
    monkeypatch.setattr("server.get_or_create_api_key", lambda: "k")
    c = TestClient(server.app)
    c.headers.update({"X-API-Key": "k"})
    return c


def test_connect_checks_then_keeps_it_encrypted(client, servers):
    r = client.post("/api/email-account", json={"label": "work", "address": "tracy@mybusiness.co.za",
                                                "password": "app-password"})
    assert r.json() == {"connected": True, "label": "work", "address": "tracy@mybusiness.co.za"}
    from core.oauth.manager import get_connection, _get_conn
    assert get_connection("email", "work")["token_data"]["credentials"]["imap_host"] == "mail.mybusiness.co.za"
    conn = _get_conn()
    stored = conn.execute("SELECT token_data FROM connections WHERE service = 'email'").fetchone()[0]
    conn.close()
    assert "app-password" not in stored  # encrypted


def test_connect_refuses_details_that_dont_work(client, servers):
    servers.fail_login = True
    r = client.post("/api/email-account", json={"label": "work", "address": "tracy@mybusiness.co.za",
                                                "password": "wrong"})
    assert r.status_code == 400 and "app password" in r.json()["detail"]
    from core.oauth.manager import get_connection
    assert get_connection("email", "work") is None  # not kept


def test_the_old_sign_in_address_explains_the_form(client):
    r = client.get("/api/integrations/email/authorize?label=work", headers={"Accept": "text/html"},
                   follow_redirects=False)
    assert r.status_code == 400 and "fill in the form" in r.text


# ── One approval, which then really happens ───────────────────────────

def test_approvals_say_what_will_happen():
    text = agent.describe_action("send_email", {"to": "sam@x.com", "subject": "Hi", "body": "Hello Sam"})
    assert text == "Send an email to sam@x.com\nSubject: Hi\n\nHello Sam"
    assert agent.describe_action("post_to_facebook", {"content": "Mince pies!"}) == "Post to Facebook:\n\nMince pies!"
    assert agent.describe_action("write_docx", {"path": "a.docx"}) == "Use write_docx (path: a.docx)"


def test_an_approved_facebook_post_is_posted(monkeypatch):
    security.set_tier(security.PermissionTier.SEMI_AUTONOMOUS.value)
    posted = []

    async def fake_token(service, label):
        return {"token_data": {"access_token": "t"}}, None

    async def fake_api(token_data, method, url):  # instead of Facebook itself
        posted.append(url)
        return {"id": "p1"}
    monkeypatch.setattr(integrations, "_ensure_token", fake_token)
    monkeypatch.setattr(integrations, "_call_api", fake_api)
    held = security.intercept_action("oauth_access", "Post", {"tool": "post_to_facebook",
                                                              "args": {"page_id": "1", "content": "Hi"}})
    conn = security._get_conn()
    conn.execute("UPDATE approvals SET status = 'approved' WHERE id = ?", (held["approval_id"],))
    conn.commit()
    conn.close()
    out = asyncio.run(security.re_execute_approval(held["approval_id"]))
    assert json.loads(out["result"]["content"]) == {"id": "p1"}  # before: it asked again instead
    assert len(posted) == 1 and "/1/feed?message=Hi" in posted[0]
    assert security.get_pending_approvals() == []


def test_team_members_can_be_given_email_sending():
    from core import team
    assert team.TOOL_CHOICES["email_send"]["tools"] == ["send_email", "reply_to_email", "save_email_draft"]
    m = team.save({"name": "Sipho", "job": "Answers customer emails.", "tools": ["email_read", "email_send"]})
    assert m["auto_tools"] == []  # asks first unless the person says otherwise
