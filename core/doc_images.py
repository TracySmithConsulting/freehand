"""Making pictures, and putting pictures into documents (Nov 2026).

FreeHand makes a picture with whichever of the person's AI services can:
Agnes first (its image model may be free), then MiniMax (image-01), then
OpenRouter (Google's Gemini image model), using the keys already set up
under AI model. Settings can change the order ("image_service") and the
models ("image_models": {"agnes": ..., "minimax": ..., "openrouter": ...}).

A picture can also be one the person put in the work folder, such as a
photo. Either way it can go into a Word document (after a paragraph) or a
PowerPoint (on a slide), in a NEW copy of the file, with alt text that
the AI model writes by looking at the actual picture, not the request.
"""
from __future__ import annotations

import base64
import io
import logging
import re
from pathlib import Path
from typing import List, Optional, Tuple

import aiohttp

from core import documents
from core.documents import Refused

log = logging.getLogger("freehand.images")

MODELS = {"agnes": "agnes-image-2.1-flash", "minimax": "image-01", "openrouter": "google/gemini-2.5-flash-image"}
ORDER = ("agnes", "minimax", "openrouter")
SIZES = {"16:9": "1792x1024", "1:1": "1024x1024", "4:3": "1536x1152", "3:4": "1152x1536", "9:16": "1024x1792"}
PICTURE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"}
TIMEOUT = aiohttp.ClientTimeout(total=180)


def _settings() -> dict:
    try:
        from core import agent_config
        return agent_config._load_settings()
    except Exception:
        return {}


def services() -> List[Tuple[str, str, str]]:
    """(service, base_url, key) for each image service the person has set up,
    in the order to try them."""
    from core import llm_providers
    from core.agent_config import get_llm_chain
    found = {}
    for cfg in get_llm_chain():
        provider = str(cfg.get("provider") or "").lower()
        base = llm_providers.base_url_for(provider, cfg.get("base_url", "")).rstrip("/")
        key = str(cfg.get("api_key") or "")
        if not key:
            continue
        if provider == "agnes" or "agnes-ai" in base:
            found.setdefault("agnes", (base, key))
        elif "minimax" in base:
            found.setdefault("minimax", ("https://api.minimax.io/v1" if "minimax.io" in base else base, key))
        elif provider == "openrouter" or "openrouter.ai" in base:
            found.setdefault("openrouter", (base, key))
    first = str(_settings().get("image_service") or "").lower()
    order = ([first] if first in ORDER else []) + [s for s in ORDER if s != first]
    return [(s, *found[s]) for s in order if s in found]


def _model(service: str) -> str:
    chosen = _settings().get("image_models") or {}
    return str(chosen.get(service) or MODELS[service]) if isinstance(chosen, dict) else MODELS[service]


async def _fetch(session, url: str) -> bytes:
    async with session.get(url) as r:
        if r.status != 200:
            raise RuntimeError(f"the picture couldn't be downloaded ({r.status})")
        return await r.read()


async def _agnes(session, base, key, prompt, aspect) -> bytes:
    body = {"model": _model("agnes"), "prompt": prompt, "n": 1, "size": SIZES.get(aspect, "1024x1024")}
    headers = {"Authorization": f"Bearer {key}"}
    for attempt in (body, {k: v for k, v in body.items() if k != "size"}):
        async with session.post(f"{base}/images/generations", json=attempt, headers=headers) as r:
            data = await r.json(content_type=None)
            if r.status == 400 and "size" in attempt:
                continue  # a size it doesn't offer: let it choose
            if r.status != 200:
                raise RuntimeError(_said(data, r.status))
        item = (data.get("data") or [{}])[0]
        if item.get("b64_json"):
            return base64.b64decode(item["b64_json"])
        if item.get("url"):
            return await _fetch(session, item["url"])
        raise RuntimeError("no picture came back")
    raise RuntimeError("it didn't accept the request")


async def _minimax(session, base, key, prompt, aspect) -> bytes:
    body = {"model": _model("minimax"), "prompt": prompt[:1500], "aspect_ratio": aspect if aspect in SIZES else "1:1",
            "response_format": "base64", "n": 1}
    async with session.post(f"{base}/image_generation", json=body, headers={"Authorization": f"Bearer {key}"}) as r:
        data = await r.json(content_type=None)
    status = (data.get("base_resp") or {}).get("status_code", 0) if isinstance(data, dict) else -1
    images = ((data.get("data") or {}).get("image_base64") or []) if isinstance(data, dict) else []
    if r.status != 200 or status or not images:
        raise RuntimeError(_said(data, r.status))
    return base64.b64decode(images[0])


async def _openrouter(session, base, key, prompt, aspect) -> bytes:
    body = {"model": _model("openrouter"), "messages": [{"role": "user", "content": prompt}],
            "modalities": ["image", "text"], "image_config": {"aspect_ratio": aspect if aspect in SIZES else "1:1"}}
    async with session.post(f"{base}/chat/completions", json=body, headers={"Authorization": f"Bearer {key}"}) as r:
        data = await r.json(content_type=None)
    if r.status != 200:
        raise RuntimeError(_said(data, r.status))
    message = ((data.get("choices") or [{}])[0].get("message") or {})
    for image in message.get("images") or []:
        url = (image.get("image_url") or {}).get("url", "")
        if url.startswith("data:"):
            return base64.b64decode(url.split(",", 1)[1])
        if url:
            return await _fetch(session, url)
    raise RuntimeError("no picture came back")


def _said(data, status) -> str:
    """The service's reason, briefly, without echoing anything long."""
    msg = ""
    if isinstance(data, dict):
        err = data.get("error")
        msg = (err.get("message") if isinstance(err, dict) else err) or \
              (data.get("base_resp") or {}).get("status_msg") or data.get("message") or ""
    return f"{status}: {str(msg)[:160]}" if msg else f"it answered {status}"


MAKERS = {"agnes": _agnes, "minimax": _minimax, "openrouter": _openrouter}
NAMES = {"agnes": "Agnes", "minimax": "MiniMax", "openrouter": "OpenRouter"}


async def make(prompt: str, aspect: str = "16:9") -> Tuple[bytes, str]:
    """A PNG from the first service that makes one, and which service."""
    from PIL import Image
    from core import ai_use
    prompt = " ".join(str(prompt or "").split())
    if not prompt:
        raise Refused("Say what the picture should show.")
    over = ai_use.refusal()
    if over:
        raise Refused(over)
    available = services()
    if not available:
        raise Refused("None of your AI services can make pictures. Agnes, MiniMax and OpenRouter can: add one "
                      "under AI model, as the main model or a backup.")
    reasons = []
    async with aiohttp.ClientSession(timeout=TIMEOUT) as session:
        for service, base, key in available:
            try:
                raw = await MAKERS[service](session, base, key, prompt, aspect)
                picture = Image.open(io.BytesIO(raw)).convert("RGB")
            except Exception as e:  # this service couldn't: try the next
                reasons.append(f"{NAMES[service]} ({str(e)[:120]})")
                log.info("image from %s failed: %s", service, e)
                continue
            out = io.BytesIO()
            picture.save(out, "PNG")
            ai_use.count({"calls": 1, "prompt_tokens": len(prompt) // 4, "completion_tokens": 0})
            return out.getvalue(), NAMES[service]
    raise Refused("No picture could be made: " + "; ".join(reasons) + ".")


def _slug(text: str) -> str:
    words = re.findall(r"[A-Za-z0-9]+", text)[:6]
    return " ".join(words) or "picture"


async def add_picture(prompt: str = "", picture: str = "", file: str = "", after_paragraph=None, slide=None,
                      alt_text: str = "", aspect: str = "16:9", output: str = "") -> dict:
    """Make a picture (or use one from the work folder), save it, and if a
    document is named, put it into a new copy of that document."""
    place = documents.folder()
    documents._owner()
    if not place:
        raise Refused(documents.NO_FOLDER)
    made_by = ""
    if picture:
        source = documents.resolve(picture)
        if source.suffix.lower() not in PICTURE_SUFFIXES:
            raise Refused("That isn't a picture FreeHand can use (PNG, JPEG, GIF, BMP or WebP).")
        data = source.read_bytes()
        saved = source
    elif prompt:
        data, made_by = await make(prompt, aspect)
        saved = documents.new_path(place / f"{_slug(prompt)}.png")
        saved.write_bytes(data)
    else:
        raise Refused("Say what picture to make, or which picture in the work folder to use.")
    alt = alt_text.strip()
    if not alt:
        described = await documents.describe(data, prompt)
        alt = described if described is not None else (prompt[:300] if prompt else "")
    result = {"picture": documents._rel(saved), "alt_text": alt or "Decorative",
              **({"made_by": made_by} if made_by else {})}
    if not file:
        return result
    target = documents.resolve(file)
    if target.suffix.lower() == ".docx":
        result["saved_as"] = _into_docx(target, data, alt, after_paragraph, output)
    elif target.suffix.lower() == ".pptx":
        result["saved_as"] = _into_pptx(target, data, alt, slide, output)
    else:
        raise Refused("Pictures go into Word documents and PowerPoints.")
    result["original"] = f"\"{documents._rel(target)}\" is unchanged."
    result["check"] = "Check the alt text says what matters about the picture, and use document_look to see the page."
    return result


def _into_docx(target: Path, data: bytes, alt: str, after, output: str) -> str:
    from docx import Document
    from docx.shared import Inches
    doc = Document(str(target))
    section = doc.sections[-1]
    room = (section.page_width - section.left_margin - section.right_margin) if section.page_width else Inches(6)
    paragraph = doc.add_paragraph()
    paragraph.add_run().add_picture(io.BytesIO(data), width=min(room, Inches(6)))
    if after is not None:
        try:
            anchor = doc.paragraphs[int(after)]
        except (ValueError, TypeError, IndexError):
            raise Refused(f"There's no paragraph {after}.")
        anchor._p.addnext(paragraph._p)
    for docpr in paragraph._p.iter(f"{{{documents.WP}}}docPr"):
        docpr.set("descr", alt or "Decorative")
    out = documents.output_for(target, "with picture", name=output)
    doc.save(str(out))
    return documents._rel(out)


def _into_pptx(target: Path, data: bytes, alt: str, slide, output: str) -> str:
    from PIL import Image
    from pptx import Presentation
    from pptx.util import Emu
    prs = Presentation(str(target))
    if slide:
        try:
            page = prs.slides[int(slide) - 1]
            if int(slide) < 1:
                raise IndexError
        except (ValueError, TypeError, IndexError):
            raise Refused(f"There's no slide {slide}.")
    else:
        layout = next((l for l in prs.slide_layouts if l.name.lower() == "title only"), prs.slide_layouts[-1])
        page = prs.slides.add_slide(layout)
        if page.shapes.title is not None:
            page.shapes.title.text = alt[:80] if alt else "Picture"
    w, h = Image.open(io.BytesIO(data)).size
    top = Emu(int(prs.slide_height * 0.22))
    room_w, room_h = int(prs.slide_width * 0.9), int(prs.slide_height - top - prs.slide_height * 0.05)
    scale = min(room_w / w, room_h / h)
    pic_w, pic_h = int(w * scale), int(h * scale)
    pic = page.shapes.add_picture(io.BytesIO(data), Emu((prs.slide_width - pic_w) // 2), top, Emu(pic_w), Emu(pic_h))
    pic._element._nvXxPr.cNvPr.set("descr", alt or "Decorative")
    out = documents.output_for(target, "with picture", name=output)
    prs.save(str(out))
    return documents._rel(out)
