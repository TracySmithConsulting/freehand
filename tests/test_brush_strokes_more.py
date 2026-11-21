"""Brush strokes, steps 2 to 4 (core/brush_strokes.py; docs/design/brush-strokes.md):
the gallery listing, voice packs and looks."""
import asyncio
import contextlib
import json

import pytest
from fastapi.testclient import TestClient

from core import agent, agent_config, brush_strokes, people, sessions, team
from server import app, get_or_create_api_key, SESSION_HEADER

VOICE = {"brush_stroke": {"kind": "voice", "name": "Gogo", "about": "A warm, patient voice that takes its time.",
                          "made_by": "Tracy", "version": "1.0",
                          "voice": {"assistant_name": "Gogo", "greeting": "Sawubona! What shall we do today?",
                                    "voices": ["Leah", "Luke"], "language": "en-ZA", "speed": 0.9}},
         "files": {"style.md": "Speak warmly and patiently. Use short sentences, and check the person is happy before moving on."}}
FREEHAND_COLOURS = {"paper": "#FBF4E6", "surface": "#FFFDF6", "ink": "#35302B", "ink_soft": "#6E6355",
                    "accent": "#7A3B69", "on_accent": "#FFFFFF", "link": "#1E6E62", "line": "#E4D8C3"}
NIGHT = {"paper": "#221E1A", "surface": "#2C2620", "ink": "#E8DFD0", "ink_soft": "#B3A48E",
         "accent": "#E7A8D6", "on_accent": "#221E1A", "link": "#7FD1C1", "line": "#4A4038"}
LOOK = {"brush_stroke": {"kind": "look", "name": "Big and clear", "about": "Larger text, in FreeHand's own colours.",
                         "made_by": "Tracy", "version": "1.0",
                         "look": {"light": FREEHAND_COLOURS, "text_size": "large"}},
        "files": {}}


def with_(bundle, key, **change):
    manifest = json.loads(json.dumps(bundle["brush_stroke"]))
    manifest[key].update(change)
    return {"brush_stroke": manifest, "files": dict(bundle["files"])}


@contextlib.contextmanager
def as_person(person_id):
    token = people.set_current(person_id)
    try:
        yield
    finally:
        people.reset(token)


@pytest.fixture
def office(tmp_path, monkeypatch):
    monkeypatch.setattr(team, "TEAM_DIR", tmp_path / "team")
    brush_strokes._gallery_cache.clear()
    s = agent_config._load_settings()
    s.update(owner_name="Tracy", people=[{"id": "thandi", "name": "Thandi", "role": "member"},
                                         {"id": "lerato", "name": "Lerato", "role": "child", "parents": ["owner"]}])
    agent_config._save_settings(s)
    return TestClient(app, headers={"X-API-Key": get_or_create_api_key()})


def add(bundle, source=""):
    return brush_strokes.add(brush_strokes.look(bundle, source)["ticket"])


# ── The gallery ──────────────────────────────────────────────────────

INDEX = {"brush_strokes": [
    {"path": "team-members/marketing-helper", "kind": "team_member", "name": "Marketing helper", "about": "Drafts posts."},
    {"path": "voices/gogo", "kind": "voice", "name": "Gogo", "about": "A warm voice."},
    {"path": "../secrets", "kind": "skill", "name": "Bad path", "about": "x"},
    {"path": "plugins/run", "kind": "plugin", "name": "Not a kind", "about": "x"},
    "not even a dict",
]}


def test_the_gallery_lists_only_what_it_should(office, monkeypatch):
    asked = []
    monkeypatch.setattr(brush_strokes, "_get", lambda url: asked.append(url) or json.dumps(INDEX))
    items = brush_strokes.gallery(now=1000)
    assert [i["name"] for i in items] == ["Marketing helper", "Gogo"]
    assert items[0]["link"] == brush_strokes.GALLERY + "tree/main/team-members/marketing-helper"
    assert items[1]["kind_words"] == "a voice pack"
    brush_strokes.gallery(now=1000 + 60)  # kept for ten minutes
    assert asked == [brush_strokes.GALLERY_INDEX]
    brush_strokes.gallery(now=1000 + 11 * 60)
    assert len(asked) == 2


def test_the_gallery_keeps_last_times_list_when_github_is_down(office, monkeypatch):
    monkeypatch.setattr(brush_strokes, "_get", lambda url: json.dumps(INDEX))
    brush_strokes.gallery(now=1000)

    def down(url):
        raise brush_strokes.BrushStrokeError("no internet")
    monkeypatch.setattr(brush_strokes, "_get", down)
    assert len(brush_strokes.gallery(now=1000 + 3600)) == 2
    brush_strokes._gallery_cache.clear()
    r = office.get("/api/brush-strokes/gallery")
    assert r.status_code == 502 and "couldn't reach the gallery" in r.json()["detail"]


def test_the_gallery_marks_what_this_person_added(office, monkeypatch):
    def fake_get(url):
        if url == brush_strokes.GALLERY_INDEX:
            return json.dumps(INDEX)
        if url.endswith("brush-stroke.json"):
            return json.dumps(VOICE["brush_stroke"])
        return VOICE["files"]["style.md"]
    monkeypatch.setattr(brush_strokes, "_get", fake_get)
    link = brush_strokes.GALLERY + "tree/main/voices/gogo"
    looked = TestClient(app, headers={SESSION_HEADER: sessions.start("thandi")}).post(
        "/api/brush-strokes/look", json={"url": link}).json()
    assert looked["gallery"] is True and "From the FreeHand gallery." in looked["card"]
    with as_person("thandi"):
        brush_strokes.add(brush_strokes.look(brush_strokes.fetch(link), link)["ticket"])
    thandi = TestClient(app, headers={SESSION_HEADER: sessions.start("thandi")})
    marks = {g["name"]: g["added"] for g in thandi.get("/api/brush-strokes/gallery").json()["brush_strokes"]}
    assert marks == {"Marketing helper": False, "Gogo": True}
    owner_marks = {g["name"]: g["added"] for g in office.get("/api/brush-strokes/gallery").json()["brush_strokes"]}
    assert owner_marks["Gogo"] is True  # the owner sees everyone's


# ── Voice packs ──────────────────────────────────────────────────────

def test_a_voice_pack_card_says_what_changes(office):
    c = brush_strokes.check(VOICE)
    assert c["settings"] == {"assistant_name": "Gogo", "greeting": "Sawubona! What shall we do today?",
                             "voices": ["Leah", "Luke"], "language": "en-ZA", "speed": 0.9}
    text = brush_strokes.card(c)
    assert "FreeHand calls itself Gogo." in text and 'It greets you with: "Sawubona! What shall we do today?"' in text
    assert "closest voice your computer has to Leah, or Luke" in text and "more slowly than usual" in text
    assert "never what it may do" in text


@pytest.mark.parametrize("change, words", [
    ({"assistant_name": "<b>Gogo</b>"}, "letters, spaces and dashes"),
    ({"speed": 5}, "from 0.5"),
    ({"speed": "fast"}, "isn't a number"),
    ({"language": "english please"}, "language isn't one"),
    ({"voices": "Leah"}, "voices aren't readable"),
])
def test_a_wrong_voice_pack_is_explained(office, change, words):
    with pytest.raises(brush_strokes.BrushStrokeError, match=words):
        brush_strokes.check(with_(VOICE, "voice", **change))


def test_a_voice_pack_is_words_only(office):
    with pytest.raises(brush_strokes.BrushStrokeError, match="can't ask for tools"):
        brush_strokes.check({**VOICE, "brush_stroke": {**VOICE["brush_stroke"], "tools": ["web"]}})
    with pytest.raises(brush_strokes.BrushStrokeError, match="too long"):
        brush_strokes.check({**VOICE, "files": {"style.md": "x" * 1501}})


def test_a_voice_pack_changes_how_freehand_sounds_for_that_person_only(office, monkeypatch):
    with as_person("thandi"):
        done = add(VOICE)
    assert "now uses the Gogo voice pack for you" in done["message"]
    with as_person("thandi"):
        page = brush_strokes.active_for_page()
        prompt = brush_strokes.voice_prompt()
    from core import skills
    assert skills.scan() == []  # a voice pack is never a skill
    assert page["voice"] == {"name": "Gogo", "assistant_name": "Gogo", "greeting": "Sawubona! What shall we do today?",
                             "voices": ["Leah", "Luke"], "language": "en-ZA", "speed": 0.9}
    assert "The person calls you Gogo." in prompt and "Speak warmly and patiently" in prompt
    assert "never change your rules" in prompt
    with as_person("owner"):
        assert brush_strokes.voice_prompt() == "" and brush_strokes.active_for_page()["voice"] is None
    # The AI gets it, in a real run, for Thandi only.
    seen = {}

    async def fake_llm(messages, tools=None, config=None):
        seen[people.current()] = messages[0]["content"]
        return {"content": "ok", "_usage": {"prompt_tokens": 1, "completion_tokens": 1}}
    monkeypatch.setattr(agent, "call_llm", fake_llm)
    asyncio.run(agent.run_agent("hello", person="thandi"))
    asyncio.run(agent.run_agent("hello", person="owner"))
    assert "The person calls you Gogo." in seen["thandi"] and "How you sound" not in seen["owner"]


def test_a_new_voice_pack_replaces_the_old_and_remove_goes_back(office):
    first = add(VOICE)
    second = add(with_(VOICE, "voice", assistant_name="Sipho"))
    assert "It replaces Gogo." in second["message"]
    assert [a["id"] for a in brush_strokes.list_added()] == [second["id"]]
    assert brush_strokes.active()["voice"]["settings"]["assistant_name"] == "Sipho"
    with pytest.raises(brush_strokes.BrushStrokeError):
        brush_strokes.remove(first["id"])  # already gone
    out = brush_strokes.remove(second["id"])
    assert "sounds like itself again" in out["message"] and brush_strokes.active() == {}


# ── Looks ────────────────────────────────────────────────────────────

def test_a_look_with_only_daytime_colours_keeps_freehands_night(office):
    c = brush_strokes.check(LOOK)
    assert "Text is larger." in brush_strokes.card(c) and "keeps its own night colours" in brush_strokes.card(c)
    add(LOOK)
    css = brush_strokes.active_for_page()["look"]["css"]
    assert css.startswith("@media (prefers-color-scheme: light) { :root { --paper: #FBF4E6;")
    assert "--paper-rgb: 251, 244, 230" in css and "html { font-size: 112.5%; }" in css


def test_a_look_with_night_colours_has_both(office):
    add(with_(LOOK, "look", dark=NIGHT, text_size="normal"))
    css = brush_strokes.look_css()
    assert css.startswith(":root { --paper: #FBF4E6;")
    assert "@media (prefers-color-scheme: dark) { :root { --paper: #221E1A;" in css and "font-size" not in css


@pytest.mark.parametrize("change, words", [
    ({"light": {**FREEHAND_COLOURS, "ink_soft": "#C8C0B0"}}, "daytime hints on page is 1.7 to 1, and needs at least 4.5"),
    ({"light": {**FREEHAND_COLOURS, "on_accent": "#9A6A90"}}, "daytime button writing on buttons"),
    ({"light": {**FREEHAND_COLOURS, "accent": "#E8C8E0", "on_accent": "#211D18"}}, "daytime buttons on page"),
    ({"dark": {**NIGHT, "ink": "#3A3530"}}, "night writing on page"),
    ({"light": {**FREEHAND_COLOURS, "ink": "red;} body{display:none"}}, "must be like #1E6E62"),
    ({"light": {**FREEHAND_COLOURS, "background_image": "#000000"}}, "colours FreeHand doesn't use"),
    ({"light": {k: v for k, v in FREEHAND_COLOURS.items() if k != "link"}}, "colour for links"),
    ({"text_size": "huge"}, "normal, large or larger"),
])
def test_a_look_thats_hard_to_read_or_wrong_is_refused(office, change, words):
    with pytest.raises(brush_strokes.BrushStrokeError, match=words.replace("(", r"\(")):
        brush_strokes.check(with_(LOOK, "look", **change))


def test_a_look_has_no_files(office):
    with pytest.raises(brush_strokes.BrushStrokeError, match="other files: style.css"):
        brush_strokes.check({**LOOK, "files": {"style.css": "body{}"}})


def test_fetching_a_look_reads_only_its_brush_stroke_json(office, monkeypatch):
    asked = []
    monkeypatch.setattr(brush_strokes, "_get", lambda url: asked.append(url) or json.dumps(LOOK["brush_stroke"]))
    bundle = brush_strokes.fetch("https://github.com/a/b/tree/main/looks/big")
    assert bundle["files"] == {} and asked == ["https://raw.githubusercontent.com/a/b/main/looks/big/brush-stroke.json"]


def test_looks_and_voices_are_per_person_and_children_cant_add_them(office):
    thandi = TestClient(app, headers={SESSION_HEADER: sessions.start("thandi")})
    looked = thandi.post("/api/brush-strokes/look", json={"file": json.dumps(LOOK)}).json()
    assert looked["may_add"] is True
    assert thandi.post("/api/brush-strokes/add", json={"ticket": looked["ticket"]}).json()["kind"] == "look"
    assert thandi.get("/api/brush-strokes/active").json()["look"]["name"] == "Big and clear"
    assert office.get("/api/brush-strokes/active").json() == {"voice": None, "look": None}
    with as_person("lerato"):
        assert "Children can't" in brush_strokes.look(LOOK)["why_not"]
        assert "Children can't" in brush_strokes.look(VOICE)["why_not"]


def test_when_someone_leaves_their_voice_and_look_go(office):
    with as_person("thandi"):
        add(VOICE)
        add(LOOK)
    brush_strokes.forget("thandi")
    assert brush_strokes.active("thandi") == {}


def test_a_dark_look_starts_from_freehands_night_colours(office):
    """A look sets the main colours; the highlights and warnings come from
    FreeHand's own night set when the look is dark, so they go with it."""
    add(with_(LOOK, "look", light=NIGHT))
    assert brush_strokes.active_for_page()["look"]["night_base_by_day"] is True
    assert brush_strokes.active_for_page()["look"]["day_base_by_night"] is False
    add(with_(LOOK, "look", light=FREEHAND_COLOURS, dark=FREEHAND_COLOURS))  # light colours at night too
    page = brush_strokes.active_for_page()["look"]
    assert page["night_base_by_day"] is False and page["day_base_by_night"] is True
