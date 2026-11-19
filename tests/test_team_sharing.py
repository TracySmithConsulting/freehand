"""Team members per person, and sharing them (core/team.py).

Each team member is its maker's. The owner can share theirs with everyone
or with named people; members can share theirs only if the owner allows
it. Someone using a shared team member uses it as themselves, and sees
only their own requests in its log."""
import contextlib
import json

import pytest
from fastapi.testclient import TestClient

from core import agent_config, leaving, people, scheduler, sessions, team
from server import app, get_or_create_api_key, SESSION_HEADER

WRITER = {"name": "Writer", "job": "Drafts our social media posts.", "tools": ["web"]}


@contextlib.contextmanager
def as_person(person_id):
    token = people.set_current(person_id)
    try:
        yield
    finally:
        people.reset(token)


@pytest.fixture
def office():
    s = agent_config._load_settings()
    s.update(owner_name="Tracy", people=[{"id": "thandi", "name": "Thandi", "role": "member"},
                                         {"id": "sam", "name": "Sam", "role": "member"}])
    agent_config._save_settings(s)
    owner = TestClient(app, headers={"X-API-Key": get_or_create_api_key()})
    thandi = TestClient(app, headers={SESSION_HEADER: sessions.start("thandi")})
    sam = TestClient(app, headers={SESSION_HEADER: sessions.start("sam")})
    return owner, thandi, sam


def names(client):
    r = client.get("/api/team")
    assert r.status_code == 200, r.text
    return [m["name"] for m in r.json()["members"]]


def test_a_team_member_from_before_people_is_the_owners_alone(office):
    owner, thandi, _ = office
    team.TEAM_DIR.mkdir(parents=True, exist_ok=True)
    (team.TEAM_DIR / "writer.json").write_text(json.dumps({"id": "writer", **WRITER}), encoding="utf-8")
    assert names(owner) == ["Writer"]
    assert names(thandi) == []
    with as_person("thandi"):
        assert team.get("writer") is None
        assert team.addressed("Writer, draft a post") == (None, "Writer, draft a post")


def test_the_owner_shares_with_everyone(office):
    owner, thandi, sam = office
    owner.post("/api/team", json={**WRITER, "shared": ["everyone"]})
    view = thandi.get("/api/team").json()["members"][0]
    assert (view["name"], view["mine"], view["maker_name"]) == ("Writer", False, "Tracy")
    assert names(sam) == ["Writer"]
    with as_person("thandi"):
        member, rest = team.addressed("Writer, draft a post about Friday")
        assert member["id"] == "writer" and rest == "draft a post about Friday"
        assert "Writer" in json.dumps(team.ask_schema(team.members()))


def test_the_owner_shares_with_one_person(office):
    owner, thandi, sam = office
    owner.post("/api/team", json={**WRITER, "shared": ["thandi"]})
    assert names(thandi) == ["Writer"] and names(sam) == []


def test_only_the_maker_changes_pauses_or_deletes(office):
    owner, thandi, _ = office
    owner.post("/api/team", json={**WRITER, "shared": ["everyone"]})
    for r in (thandi.put("/api/team/writer", json={**WRITER, "job": "Something else"}),
              thandi.post("/api/team/writer/pause", json={"paused": True}),
              thandi.delete("/api/team/writer")):
        assert r.status_code == 403 and "Tracy's, so only Tracy can change them" in r.json()["detail"]
    assert team.get("writer")["job"] == WRITER["job"] and not team.get("writer")["paused"]


def test_a_member_has_their_own_team_members(office):
    owner, thandi, sam = office
    r = thandi.post("/api/team", json={"name": "Bookkeeper", "job": "Checks invoices."})
    assert r.status_code == 200, r.text
    assert r.json()["person"] == "thandi" and r.json()["mine"] is True
    assert names(thandi) == ["Bookkeeper"]
    assert names(owner) == [] and names(sam) == []
    assert thandi.post("/api/team/bookkeeper/pause", json={"paused": True}).json()["paused"] is True
    assert thandi.delete("/api/team/bookkeeper").status_code == 200


def test_members_share_only_if_the_owner_allows(office):
    owner, thandi, sam = office
    r = thandi.post("/api/team", json={"name": "Bookkeeper", "job": "Checks invoices.", "shared": ["everyone"]})
    assert r.status_code == 400 and "Ask Tracy to switch it on under People" in r.json()["detail"]
    assert thandi.get("/api/team").json()["may_share"] is False
    assert owner.post("/api/people/team-sharing", json={"allowed": True}).json()["team_sharing_by_members"] is True
    assert thandi.get("/api/team").json()["may_share"] is True
    r = thandi.post("/api/team", json={"name": "Bookkeeper", "job": "Checks invoices.", "shared": ["sam"]})
    assert r.status_code == 200, r.text
    assert names(sam) == ["Bookkeeper"] and names(owner) == []
    # Switched off: her share stops at once.
    owner.post("/api/people/team-sharing", json={"allowed": False})
    assert names(sam) == []
    assert names(thandi) == ["Bookkeeper"]


def test_a_member_cant_change_the_sharing_setting(office):
    _, thandi, _ = office
    assert thandi.post("/api/people/team-sharing", json={"allowed": True}).status_code == 403


def test_the_same_name_is_fine_when_its_not_shared(office):
    owner, thandi, _ = office
    owner.post("/api/team", json=WRITER)
    r = thandi.post("/api/team", json={**WRITER, "job": "Writes Thandi's newsletters."})
    assert r.status_code == 200, r.text
    assert r.json()["id"] == "writer-2"
    with as_person("thandi"):
        assert team.addressed("Writer, hello")[0]["job"] == "Writes Thandi's newsletters."
    with as_person("owner"):
        assert team.addressed("Writer, hello")[0]["job"] == WRITER["job"]


def test_a_shared_name_cant_be_taken_again(office):
    owner, thandi, _ = office
    owner.post("/api/team", json={**WRITER, "shared": ["everyone"]})
    r = thandi.post("/api/team", json=WRITER)
    assert r.status_code == 400 and "already a team member you can use" in r.json()["detail"]


def test_each_person_sees_only_their_own_requests(office):
    owner, thandi, _ = office
    owner.post("/api/team", json={**WRITER, "shared": ["everyone"]})
    writer = team.get("writer")
    with as_person("owner"):
        team.record(writer, "the owner's secret plan", {"text": "done"})
    with as_person("thandi"):
        team.record(writer, "Thandi's post", {"text": "done"})
    assert [r["asked"] for r in thandi.get("/api/team").json()["members"][0]["recent"]] == ["Thandi's post"]
    assert [r["asked"] for r in owner.get("/api/team").json()["members"][0]["recent"]] == ["the owner's secret plan"]


def test_a_job_with_a_team_member_no_longer_shared_doesnt_run_as_them(office):
    owner, thandi, _ = office
    owner.post("/api/team", json={**WRITER, "shared": ["thandi"]})
    owner.put("/api/team/writer", json={**WRITER, "shared": []})
    with as_person("thandi"):
        assert scheduler._member_for({"team_member": "writer"}) is None


@pytest.mark.parametrize("keep", [True, False])
def test_when_someone_leaves_their_team_members_go_with_their_jobs(office, keep):
    owner, thandi, _ = office
    owner.post("/api/team", json={**WRITER, "shared": ["everyone"]})
    owner.post("/api/team", json={"name": "Bookkeeper", "job": "The owner's own."})
    thandi.post("/api/team", json={"name": "Bookkeeper", "job": "Checks invoices."})
    with as_person("thandi"):
        team.record(team.get("writer"), "Thandi's post", {"text": "done"})
    r = owner.request("DELETE", "/api/people/thandi", json={"keep_jobs": keep})
    assert r.status_code == 200, r.text
    left = {m["name"]: m for m in team.all_members()}
    if keep:
        assert left["Bookkeeper 2"]["person"] == "owner" and left["Bookkeeper 2"]["shared"] == []
        assert "team member is yours now" in " ".join(r.json()["done"])
    else:
        assert set(left) == {"Writer", "Bookkeeper"}
    assert not any(m.get("person") == "thandi" for m in team.all_members())
    log = (team.TEAM_DIR / "writer.log.jsonl").read_text(encoding="utf-8")
    assert "Thandi's post" not in log


def test_the_leaving_form_counts_their_team_members(office):
    _, thandi, _ = office
    thandi.post("/api/team", json={"name": "Bookkeeper", "job": "Checks invoices."})
    assert leaving.what_they_have("thandi")["team_members"] == 1
