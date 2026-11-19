"""Each person's own time zone: "every day at 9" means 9 where they are.

A member's browser sends their zone, and it's kept on them; until it has,
the owner's is theirs too. Their scheduled jobs run, and come round
again, in their zone."""
import contextlib
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from core import agent_config, people, scheduler, sessions
from server import app, get_or_create_api_key, SESSION_HEADER


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
    s.update(owner_name="Tracy", timezone="Africa/Johannesburg",
             people=[{"id": "thandi", "name": "Thandi", "role": "member"}])
    agent_config._save_settings(s)
    owner = TestClient(app, headers={"X-API-Key": get_or_create_api_key()})
    thandi = TestClient(app, headers={SESSION_HEADER: sessions.start("thandi")})
    return owner, thandi


def test_a_member_uses_the_owners_zone_until_their_browser_says(office):
    with as_person("thandi"):
        assert scheduler.local_tz() == ZoneInfo("Africa/Johannesburg")


def test_a_members_browser_sets_their_own_zone_not_the_owners(office):
    owner, thandi = office
    r = thandi.post("/api/timezone", json={"timezone": "Europe/London"})
    assert r.status_code == 200 and r.json()["changed"] is True
    assert thandi.post("/api/timezone", json={"timezone": "Europe/London"}).json()["changed"] is False
    with as_person("thandi"):
        assert scheduler.local_tz() == ZoneInfo("Europe/London")
    with as_person("owner"):
        assert scheduler.local_tz() == ZoneInfo("Africa/Johannesburg")
    assert agent_config._load_settings()["timezone"] == "Africa/Johannesburg"


def test_a_made_up_zone_is_refused(office):
    _, thandi = office
    assert thandi.post("/api/timezone", json={"timezone": "Mars/Olympus"}).status_code == 400
    assert people.timezone_of("thandi") == ""


def test_her_daily_job_at_nine_is_nine_in_london(office):
    _, thandi = office
    thandi.post("/api/timezone", json={"timezone": "Europe/London"})
    with as_person("thandi"):
        task = scheduler.create_task("Morning summary", "0 9 * * *")
    nxt = datetime.fromisoformat(task["next_run"].replace("Z", "+00:00"))
    assert nxt.astimezone(ZoneInfo("Europe/London")).hour == 9
    with as_person("owner"):
        mine = scheduler.create_task("My summary", "0 9 * * *")
    mine_next = datetime.fromisoformat(mine["next_run"].replace("Z", "+00:00"))
    assert mine_next.astimezone(ZoneInfo("Africa/Johannesburg")).hour == 9


def test_after_running_her_job_comes_round_again_in_her_zone(office, monkeypatch):
    _, thandi = office
    thandi.post("/api/timezone", json={"timezone": "Europe/London"})
    with as_person("thandi"):
        task = scheduler.create_task("Water the plants", "0 9 * * *", kind="do",
                                     action={"type": "remind", "text": "Water the plants"})
    monkeypatch.setattr(scheduler, "run_action", lambda action, person=None: "Water the plants")
    conn = scheduler._tasks_conn()
    conn.execute("UPDATE tasks SET next_run = ? WHERE id = ?", ("2026-11-19T08:59:00Z", task["id"]))
    conn.commit()
    conn.close()
    scheduler.run_due_tasks(now=datetime(2026, 11, 19, 9, 1, tzinfo=timezone.utc)) \
        if "now" in scheduler.run_due_tasks.__code__.co_varnames else scheduler.run_due_tasks()
    conn = scheduler._tasks_conn()
    (nxt,) = conn.execute("SELECT next_run FROM tasks WHERE id = ?", (task["id"],)).fetchone()
    conn.close()
    assert datetime.fromisoformat(nxt.replace("Z", "+00:00")).astimezone(ZoneInfo("Europe/London")).hour == 9
