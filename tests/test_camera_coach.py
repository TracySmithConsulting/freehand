"""The camera coach (core/camera_coach.py): the right direction for where
someone is, from their own point of view, and nothing leaves the computer.

Faces here are made up (a box and five points), so no photo is needed.
The picture from a camera isn't mirrored: someone too far to their own
right shows up on the left of the picture.
"""
import io
import re
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("onnxruntime")
Image = pytest.importorskip("PIL.Image")

from core import camera_coach as cc  # noqa: E402

W, H = 1280, 720
ROOT = Path(__file__).resolve().parent.parent


def face(cx=640, eyes_y=260, size=300, tilt_px=0, turn_px=0):
    """A face this big (pixels high), centred at cx, eyes at eyes_y."""
    w = size * 0.8
    gap = w * 0.4
    right_eye = (cx - gap / 2, eyes_y - tilt_px / 2)   # their right: the picture's left
    left_eye = (cx + gap / 2, eyes_y + tilt_px / 2)
    nose = (cx + turn_px, eyes_y + gap * 0.5)
    mouth_y = eyes_y + gap
    top = eyes_y - size * 0.4
    return {"score": 0.9, "box": (cx - w / 2, top, w, size),
            "points": [right_eye, left_eye, nose, (cx - gap / 3, mouth_y), (cx + gap / 3, mouth_y)]}


def tips(**kw):
    return cc._placing(face(**kw), W, H)


def test_well_placed_gets_no_directions():
    assert tips() == []


def test_left_and_right_are_the_persons_own():
    assert tips(cx=300) == ["Move a little to your left."]
    assert tips(cx=980) == ["Move a little to your right."]


def test_too_close_and_too_far():
    assert "You're too close. Move back a little." in tips(size=500, eyes_y=330)
    assert tips(size=110) == ["You're quite far away. Move a little closer."]


def test_too_low_and_too_high():
    assert any("low in the picture" in t for t in tips(eyes_y=450))
    assert any("high in the picture" in t for t in tips(eyes_y=150))


def test_head_tilt_names_the_shoulder():
    # Their left eye (the picture's right) lower: tilted towards their left shoulder.
    assert any("left shoulder" in t for t in tips(tilt_px=60))
    assert any("right shoulder" in t for t in tips(tilt_px=-60))
    assert tips(tilt_px=10) == []  # a little tilt is normal


def test_turned_away():
    assert any("turned a little to your left" in t for t in tips(turn_px=40))
    assert any("turned a little to your right" in t for t in tips(turn_px=-40))


def test_out_of_the_picture():
    assert tips(cx=60)[0] == "Part of your face is out of the picture."


def grey(level, face_level=None, f=None, right_cheek=None):
    g = np.full((H, W), float(level), dtype=np.float32)
    if f is not None:
        x, y, w, h = (int(v) for v in f["box"])
        g[y:y + h, x:x + w] = face_level
        if right_cheek is not None:  # their right: the picture's left of the nose
            nose_x = int(f["points"][2][0])
            g[y:y + h, x:nose_x] = right_cheek
    return g


def test_light_behind_them():
    f = face()
    assert "light is behind you" in cc._light(grey(230, 30, f), f)[0]


def test_face_too_dark_or_too_bright():
    f = face()
    assert "too dark" in cc._light(grey(40, 30, f), f)[0]
    assert "too bright" in cc._light(grey(120, 240, f), f)[0]


def test_light_from_one_side():
    f = face()
    said = cc._light(grey(120, 120, f, right_cheek=200), f)
    assert said == ["The light is mostly on your right side, so your left side is in shadow. "
                    "Move the light more in front of you."]


def test_good_light_says_nothing():
    f = face()
    assert cc._light(grey(120, 140, f), f) == []


def jpeg(colour=(20, 20, 20)):
    b = io.BytesIO()
    Image.new("RGB", (640, 480), colour).save(b, "JPEG")
    return b.getvalue()


def test_the_model_ships_and_finds_no_face_in_an_empty_room():
    assert cc.available(), "freehand/models/face_detection_yunet_2023mar.onnx is missing"
    said = cc.look(jpeg())
    assert said["faces"] == 0 and not said["ok"]
    assert said["say"].startswith("I can't see your face.")
    assert any("too dark" in t for t in said["tips"])


def test_bad_pictures_are_refused_plainly():
    assert "error" in cc.look(b"")
    assert "error" in cc.look(b"not a picture")
    assert "error" in cc.look(b"x" * (cc.MAX_IMAGE_BYTES + 1))


def test_the_model_licence_ships_with_it():
    models = ROOT / "freehand" / "models"
    assert "MIT License" in (models / "LICENSE-YuNet.txt").read_text(encoding="utf-8")
    assert '"models/*"' in (ROOT / "pyproject.toml").read_text(encoding="utf-8")


def test_the_page_keeps_it_on_this_computer_and_quiet():
    page = (ROOT / "freehand" / "static" / "index.html").read_text(encoding="utf-8")
    script = page[page.index("// ---- The camera coach"):page.index("// ---- Help on my computer")]
    # Only FreeHand's own server is asked, never an outside address.
    assert re.findall(r"apiFetch\('([^']+)'", script) == ["/api/camera/look", "/api/camera/status"]
    assert "http" not in re.sub(r"//[^\n]*", "", script)
    # Said only when it changes, or again after a while.
    assert "r.d.say !== coach.last || Date.now() - coach.lastAt > REPEAT_AFTER" in script
    # It stops when you leave its page.
    assert "classList.contains('page-off')" in script
    assert '<a href="#camera-coach">Camera coach</a>' in page


@pytest.fixture
def owner():
    from fastapi.testclient import TestClient
    from core import agent_config
    from server import app, get_or_create_api_key
    s = agent_config._load_settings()
    s.update(owner_name="Tracy", people=[{"id": "thandi", "name": "Thandi", "role": "member"}])
    agent_config._save_settings(s)
    return TestClient(app, headers={"X-API-Key": get_or_create_api_key()})


def test_the_server_looks_and_answers(owner):
    assert owner.get("/api/camera/status").json() == {"available": True}
    r = owner.post("/api/camera/look", content=jpeg(), headers={"Content-Type": "image/jpeg"})
    assert r.status_code == 200 and r.json()["faces"] == 0
    bad = owner.post("/api/camera/look", content=b"nope", headers={"Content-Type": "image/jpeg"})
    assert bad.status_code == 400 and bad.json()["detail"] == "That picture couldn't be read."


def test_everyone_on_this_freehand_can_use_it(owner):
    from fastapi.testclient import TestClient
    from core import sessions
    from server import app, SESSION_HEADER
    thandi = TestClient(app, headers={SESSION_HEADER: sessions.start("thandi")})
    assert thandi.post("/api/camera/look", content=jpeg(), headers={"Content-Type": "image/jpeg"}).status_code == 200


def test_it_needs_signing_in():
    from fastapi.testclient import TestClient
    from server import app
    assert TestClient(app).post("/api/camera/look", content=jpeg()).status_code == 401
