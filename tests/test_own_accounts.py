"""Members connecting their own accounts (core/accounts.py, server.py).

A member's account is named after them ("thandi-personal"), is theirs
from the moment they start connecting it, and only they use, test or
disconnect it. They see their own and the shared accounts, nobody else's."""
import importlib

import pytest
from fastapi.testclient import TestClient

from core import accounts, agent_config, people, sessions
from core.oauth import open_connector
from server import app, get_or_create_api_key, SESSION_HEADER

router_module = importlib.import_module("core.oauth.router")


@pytest.fixture
def office(monkeypatch):
    s = agent_config._load_settings()
    s.update(owner_name="Tracy",
             people=[{"id": "thandi", "name": "Thandi", "role": "member"},
                     {"id": "sam", "name": "Sam", "role": "member"}],
             account_owners={"oc:gmail:info": "shared"})
    agent_config._save_settings(s)
    connected = [{"service": "gmail", "label": "default", "display": "tracy@example.com", "scopes": []},
                 {"service": "gmail", "label": "info", "display": "info@example.com", "scopes": []}]
    monkeypatch.setattr(open_connector, "list_accounts", lambda: list(connected))
    started, disconnected = [], []
    monkeypatch.setattr(open_connector, "start_oauth_connection",
                        lambda service, name: started.append((service, name)) or ("https://accounts.example/consent", None))

    def disconnect_account(service, label):
        disconnected.append((service, label))
        connected[:] = [c for c in connected if (c["service"], c["label"]) != (service, label)]
        return True
    monkeypatch.setattr(open_connector, "disconnect_account", disconnect_account)
    monkeypatch.setattr(router_module, "_oc_catalogue", lambda: [])
    from core.oauth import broker
    monkeypatch.setattr(broker, "_oc_available", lambda: True)
    monkeypatch.setattr(broker, "_oc_service_known", lambda svc: svc == "gmail")
    monkeypatch.setattr(broker, "_load_shared_apps", lambda: {})
    clients = {
        "owner": TestClient(app, headers={"X-API-Key": get_or_create_api_key()}),
        "thandi": TestClient(app, headers={SESSION_HEADER: sessions.start("thandi")}),
        "sam": TestClient(app, headers={SESSION_HEADER: sessions.start("sam")}),
        "anyone": TestClient(app),
    }
    return clients, connected, started, disconnected


def connect(clients, who, label, service="gmail"):
    ticket = clients[who].post("/api/connect-ticket").json()["ticket"]
    return clients["anyone"].get(f"/api/integrations/{service}/authorize?label={label}&ticket={ticket}",
                                 follow_redirects=False)


def labels(client):
    r = client.get("/api/integrations")
    assert r.status_code == 200, r.text
    return sorted(c["label"] for g in r.json()["connections"] for c in g["connections"])


def test_a_member_connects_an_account_of_their_own(office):
    clients, connected, started, _ = office
    r = connect(clients, "thandi", "thandi-personal")
    assert r.status_code == 302 and started == [("gmail", "thandi-personal")]
    assert accounts.owner_of("oc:gmail:thandi-personal") == "thandi"
    connected.append({"service": "gmail", "label": "thandi-personal", "display": "thandi@example.com", "scopes": []})
    assert labels(clients["thandi"]) == ["info", "thandi-personal"]  # hers and the shared one
    assert labels(clients["sam"]) == ["info"]
    assert "thandi-personal" in labels(clients["owner"])


@pytest.mark.parametrize("label", ["personal", "default", "sam-work", "thandi-"])
def test_a_member_cant_take_someone_elses_name(office, label):
    clients, _, started, _ = office
    r = connect(clients, "thandi", label)
    assert r.status_code == 403 and started == []


def test_a_member_cant_reconnect_someone_elses_account(office):
    clients, _, started, _ = office
    accounts.claim("oc", "gmail", "thandi-work", "sam")  # hand-edited, say
    assert connect(clients, "thandi", "thandi-work").status_code == 403 and started == []


def test_a_ticket_is_still_once_only_for_a_member(office):
    clients, _, _, _ = office
    ticket = clients["thandi"].post("/api/connect-ticket").json()["ticket"]
    url = f"/api/integrations/gmail/authorize?label=thandi-personal&ticket={ticket}"
    assert clients["anyone"].get(url, follow_redirects=False).status_code == 302
    assert clients["anyone"].get(url, follow_redirects=False).status_code == 401


def test_the_owner_still_names_accounts_freely(office):
    clients, _, started, _ = office
    assert connect(clients, "owner", "work").status_code == 302
    assert accounts.owner_of("oc:gmail:work") == people.OWNER_ID


def test_only_the_member_tests_or_disconnects_their_account(office):
    clients, connected, _, disconnected = office
    connect(clients, "thandi", "thandi-personal")
    connected.append({"service": "gmail", "label": "thandi-personal", "display": "t@example.com", "scopes": []})
    for path in ("/api/integrations/gmail/disconnect?label=thandi-personal",
                 "/api/integrations/gmail/test?label=thandi-personal"):
        r = clients["sam"].post(path)
        assert r.status_code == 403 and "isn't yours" in r.json()["detail"]
    assert clients["thandi"].post("/api/integrations/gmail/disconnect?label=thandi-personal").status_code == 200
    assert disconnected == [("gmail", "thandi-personal")]
    assert "oc:gmail:thandi-personal" not in accounts.owners()  # nobody's any more


@pytest.mark.parametrize("label", ["default", "info"])
def test_a_member_cant_disconnect_the_owners_or_the_shared_accounts(office, label):
    clients, _, _, disconnected = office
    r = clients["thandi"].post(f"/api/integrations/gmail/disconnect?label={label}")
    assert r.status_code == 403 and disconnected == []


def test_a_member_still_cant_say_whose_an_account_is(office):
    clients, _, _, _ = office
    r = clients["thandi"].post("/api/accounts/whose", json={"account": "oc:gmail:default", "whose": "thandi"})
    assert r.status_code == 403


def test_freehand_uses_her_account_for_her_and_nobody_elses(office):
    clients, _, _, _ = office
    connect(clients, "thandi", "thandi-personal")
    assert accounts.may_use("oc:gmail:thandi-personal", "thandi")
    assert not accounts.may_use("oc:gmail:thandi-personal", "sam")
    assert not accounts.may_use("oc:gmail:thandi-personal", "owner")


def test_when_she_leaves_her_own_account_is_disconnected(office, monkeypatch):
    from core import long_memory
    monkeypatch.setattr(long_memory, "enabled", lambda: False)
    clients, connected, _, disconnected = office
    connect(clients, "thandi", "thandi-personal")
    connected.append({"service": "gmail", "label": "thandi-personal", "display": "t@example.com", "scopes": []})
    r = clients["owner"].request("DELETE", "/api/people/thandi", json={})
    assert r.status_code == 200, r.text
    assert disconnected == [("gmail", "thandi-personal")]
