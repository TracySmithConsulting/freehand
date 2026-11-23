"""The camera coach: help getting into the picture, without needing to see it.

The console sends a still from the webcam every second or two while the
coach is on, and gets back one short direction ("Move a little to your
left") plus the details. Everything happens on this computer: a small
face-finding model (YuNet, from OpenCV's model collection, MIT licence,
freehand/models/) and some arithmetic on the light. No AI service is
used, nothing is stored, and the picture never leaves the computer.

Directions are from the person's point of view, facing the camera. The
picture from the camera isn't mirrored, so someone too far to their own
right shows up on the left of the picture.
"""
from __future__ import annotations

import io
import math
from pathlib import Path
from typing import List, Optional

MODEL = Path(__file__).resolve().parent.parent / "freehand" / "models" / "face_detection_yunet_2023mar.onnx"
SIZE = 640            # the model takes a 640 x 640 picture
STRIDES = (8, 16, 32)
MIN_SCORE = 0.7
MAX_IMAGE_BYTES = 4 * 1024 * 1024

# Head-and-shoulders framing, as a share of the picture.
CENTRE_SLACK = 0.10   # face centre within 10% of the middle, side to side
EYES_HIGHEST, EYES_LOWEST = 0.25, 0.50   # eyes in the upper part of the picture
FACE_SMALLEST, FACE_BIGGEST = 0.20, 0.60  # face height against picture height
TILT_DEGREES = 10     # most people hold their head a little to one side
TURN_SHARE = 0.25     # nose off the middle of the eyes, against eye distance
TOO_DARK, TOO_BRIGHT = 60, 215
BACKLIT_GAP = 45      # background this much brighter than the face
SIDE_LIGHT_GAP = 45   # one side of the face this much brighter than the other

_session = None


def available() -> bool:
    try:
        import numpy  # noqa: F401
        import onnxruntime  # noqa: F401
        from PIL import Image  # noqa: F401
    except ImportError:
        return False
    return MODEL.is_file()


def _model():
    global _session
    if _session is None:
        import onnxruntime as ort
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 2  # a check every second or two: keep it light
        _session = ort.InferenceSession(str(MODEL), sess_options=opts, providers=["CPUExecutionProvider"])
    return _session


def _faces(rgb) -> List[dict]:
    """Faces in the picture (a numpy RGB array), biggest first, in pixels."""
    import numpy as np
    from PIL import Image

    h, w = rgb.shape[:2]
    scale = SIZE / max(h, w)
    small = Image.fromarray(rgb).resize((max(1, round(w * scale)), max(1, round(h * scale))))
    canvas = np.zeros((SIZE, SIZE, 3), dtype=np.float32)
    arr = np.asarray(small, dtype=np.float32)
    canvas[:arr.shape[0], :arr.shape[1]] = arr[:, :, ::-1]  # the model wants BGR
    blob = canvas.transpose(2, 0, 1)[None]

    session = _model()
    names = [o.name for o in session.get_outputs()]
    out = dict(zip(names, session.run(None, {session.get_inputs()[0].name: blob})))

    found = []
    for s in STRIDES:
        cols = SIZE // s
        cls = np.clip(out[f"cls_{s}"][0, :, 0], 0, 1)
        obj = np.clip(out[f"obj_{s}"][0, :, 0], 0, 1)
        score = np.sqrt(cls * obj)
        for i in np.nonzero(score >= MIN_SCORE)[0]:
            r, c = divmod(int(i), cols)
            bx, by, bw, bh = out[f"bbox_{s}"][0, i]
            cx, cy = (c + bx) * s, (r + by) * s
            fw, fh = math.exp(bw) * s, math.exp(bh) * s
            k = out[f"kps_{s}"][0, i]
            points = [((k[2 * n] + c) * s / scale, (k[2 * n + 1] + r) * s / scale) for n in range(5)]
            found.append({"score": float(score[i]),
                          "box": ((cx - fw / 2) / scale, (cy - fh / 2) / scale, fw / scale, fh / scale),
                          "points": points})
    return _keep_best(found)


def _keep_best(found: List[dict]) -> List[dict]:
    """Drop overlapping finds of the same face (non-maximum suppression)."""
    def overlap(a, b):
        ax, ay, aw, ah = a
        bx, by, bw, bh = b
        ix = max(0.0, min(ax + aw, bx + bw) - max(ax, bx))
        iy = max(0.0, min(ay + ah, by + bh) - max(ay, by))
        inter = ix * iy
        union = aw * ah + bw * bh - inter
        return inter / union if union > 0 else 0.0

    kept = []
    for f in sorted(found, key=lambda f: f["score"], reverse=True):
        if all(overlap(f["box"], k["box"]) < 0.3 for k in kept):
            kept.append(f)
    return sorted(kept, key=lambda f: f["box"][2] * f["box"][3], reverse=True)


def _brightness(grey, box=None) -> Optional[float]:
    if box is None:
        return float(grey.mean())
    h, w = grey.shape
    x, y, bw, bh = box
    x0, y0 = max(0, int(x)), max(0, int(y))
    x1, y1 = min(w, int(x + bw)), min(h, int(y + bh))
    if x1 <= x0 or y1 <= y0:
        return None
    return float(grey[y0:y1, x0:x1].mean())


def _cheeks(grey, points):
    """Brightness of the person's right and left cheek areas."""
    right_eye, left_eye, nose, right_mouth, left_mouth = points
    gap = math.dist(right_eye, left_eye)
    if gap <= 0:
        return None, None
    top = min(right_eye[1], left_eye[1]) - 0.3 * gap
    bottom = max(right_mouth[1], left_mouth[1]) + 0.1 * gap
    outer_right = min(right_eye[0], right_mouth[0]) - 0.3 * gap
    outer_left = max(left_eye[0], left_mouth[0]) + 0.3 * gap
    middle = nose[0]
    return (_brightness(grey, (outer_right, top, middle - outer_right, bottom - top)),
            _brightness(grey, (middle, top, outer_left - middle, bottom - top)))


def _light(grey, face: Optional[dict]) -> List[str]:
    """What's wrong with the light, most important first."""
    overall = _brightness(grey)
    if face is None:
        if overall < TOO_DARK:
            return ["The room is too dark for the camera. Turn on a light, or face a window."]
        return []
    x, y, w, h = face["box"]
    on_face = _brightness(grey, face["box"])
    if on_face is None:
        return []
    tips = []
    if on_face < TOO_DARK:
        background = (overall * grey.size - on_face * w * h) / max(1.0, grey.size - w * h)
        if background - on_face > BACKLIT_GAP:
            tips.append("The light is behind you, so your face is dark. Turn to face the light, "
                        "or put a lamp in front of you.")
        else:
            tips.append("Your face is too dark. Add a light in front of you, a little above the camera.")
    elif on_face > TOO_BRIGHT:
        tips.append("Your face is too bright. Move the light further away, or turn it away from you a little.")
    # One side in shadow: the cheeks either side of the nose, eyes to mouth,
    # so hair and the background don't count. The picture's left is the
    # person's right.
    their_right, their_left = _cheeks(grey, face["points"])
    if their_right is not None and their_left is not None and abs(their_right - their_left) > SIDE_LIGHT_GAP:
        side = "right" if their_right > their_left else "left"
        other = "left" if side == "right" else "right"
        tips.append(f"The light is mostly on your {side} side, so your {other} side is in shadow. "
                    f"Move the light more in front of you.")
    return tips


def _placing(face: dict, width: int, height: int) -> List[str]:
    """Where to move, most important first, from the person's point of view."""
    x, y, w, h = face["box"]
    right_eye, left_eye, nose = face["points"][0], face["points"][1], face["points"][2]
    tips = []

    if x < 0 or y < 0 or x + w > width or y + h > height:
        tips.append("Part of your face is out of the picture.")

    size = h / height
    if size > FACE_BIGGEST:
        tips.append("You're too close. Move back a little.")
    elif size < FACE_SMALLEST:
        tips.append("You're quite far away. Move a little closer.")

    across = (x + w / 2) / width
    if across < 0.5 - CENTRE_SLACK:
        tips.append("Move a little to your left.")
    elif across > 0.5 + CENTRE_SLACK:
        tips.append("Move a little to your right.")

    eyes = (right_eye[1] + left_eye[1]) / 2 / height
    if eyes > EYES_LOWEST:
        tips.append("You're low in the picture. Tilt the camera down a little, or sit up higher.")
    elif eyes < EYES_HIGHEST:
        tips.append("You're high in the picture. Tilt the camera up a little, or sit a little lower.")

    tilt = math.degrees(math.atan2(left_eye[1] - right_eye[1], left_eye[0] - right_eye[0]))
    if tilt > TILT_DEGREES:
        tips.append("Your head is tilted towards your left shoulder. Straighten up a little.")
    elif tilt < -TILT_DEGREES:
        tips.append("Your head is tilted towards your right shoulder. Straighten up a little.")

    eye_gap = math.dist(right_eye, left_eye)
    if eye_gap > 0:
        turn = (nose[0] - (right_eye[0] + left_eye[0]) / 2) / eye_gap
        if turn > TURN_SHARE:
            tips.append("You're turned a little to your left. Turn to face the camera.")
        elif turn < -TURN_SHARE:
            tips.append("You're turned a little to your right. Turn to face the camera.")
    return tips


def look(image: bytes) -> dict:
    """Check one still from the camera.

    Returns {"say": one short direction, "ok": whether all is well,
    "tips": every direction, most important first, "faces": how many}.
    """
    if not image:
        return {"error": "No picture came from the camera."}
    if len(image) > MAX_IMAGE_BYTES:
        return {"error": "The picture is too big."}
    if not available():
        return {"error": "The camera coach isn't set up in this FreeHand. Updating FreeHand should fix it."}
    import numpy as np
    from PIL import Image, UnidentifiedImageError
    try:
        picture = Image.open(io.BytesIO(image)).convert("RGB")
    except (UnidentifiedImageError, OSError):
        return {"error": "That picture couldn't be read."}
    rgb = np.asarray(picture)
    grey = np.asarray(picture.convert("L"), dtype=np.float32)
    height, width = grey.shape

    faces = _faces(rgb)
    face = faces[0] if faces else None
    tips: List[str] = []
    if face is None:
        tips.append("I can't see your face. Check the camera is pointing at you and nothing is covering it.")
        tips += _light(grey, None)
    else:
        tips += _placing(face, width, height)
        tips += _light(grey, face)
        if len(faces) > 1:
            tips.append(f"I can see {len(faces)} faces. I'm guiding the biggest one, which should be you.")

    ok = face is not None and not tips
    say = "You're well placed: centred, a good distance away, and the light is good." if ok else tips[0]
    return {"say": say, "ok": ok, "tips": tips, "faces": len(faces)}
