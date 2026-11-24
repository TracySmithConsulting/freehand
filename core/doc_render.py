"""Seeing documents: accessible PDFs, and a look check of the pages (Nov 2026).

LibreOffice (without a screen) turns a Word document or PowerPoint into a
PDF, and poppler's pdftoppm turns PDF pages into pictures. Both run on this
computer, inside FreeHand.

- save_pdf: a tagged PDF beside the original, with the document's title and
  language, so it's accessible (headings, lists, tables and alt text carry
  over from the original).
- look: pictures of a few pages or slides go to the AI model, which says how
  they look: text cut off or overlapping, hard-to-read text, pictures
  stretched or out of place, near-empty pages. One AI call for all of them.

Results go in the work folder as new files, like everything else there
(core/documents.py). The pictures of pages are temporary and not kept.
"""
from __future__ import annotations

import base64
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import List, Optional

from core import documents
from core.documents import Refused

MAX_LOOK_PAGES = 6
TIMEOUT = 180
_lock = threading.Lock()  # one LibreOffice at a time: it shares a profile

PDF_FILTERS = {
    ".docx": "pdf:writer_pdf_Export:" + json.dumps({"UseTaggedPDF": {"type": "boolean", "value": "true"},
                                                    "ExportBookmarks": {"type": "boolean", "value": "true"}}),
    ".pptx": "pdf:impress_pdf_Export:" + json.dumps({"UseTaggedPDF": {"type": "boolean", "value": "true"},
                                                     "ExportBookmarks": {"type": "boolean", "value": "true"}}),
}


def soffice() -> Optional[str]:
    return shutil.which("soffice") or shutil.which("libreoffice")


def available() -> bool:
    return bool(soffice() and shutil.which("pdftoppm"))


NOT_INSTALLED = ("Seeing documents needs LibreOffice, which isn't in this FreeHand yet. "
                 "Updating FreeHand should add it.")


def _to_pdf(source: Path, out_dir: Path) -> Path:
    """LibreOffice: source to a tagged PDF in out_dir. Raises Refused."""
    office = soffice()
    if not office:
        raise Refused(NOT_INSTALLED)
    profile = Path(tempfile.gettempdir()) / "freehand-libreoffice"
    args = [office, f"-env:UserInstallation=file://{profile.as_posix()}", "--headless", "--norestore",
            "--convert-to", PDF_FILTERS.get(source.suffix.lower(), "pdf"), "--outdir", str(out_dir), str(source)]
    with _lock:
        try:
            subprocess.run(args, capture_output=True, timeout=TIMEOUT, check=False,
                           env={**os.environ, "HOME": str(profile.parent)})
        except subprocess.TimeoutExpired:
            raise Refused("LibreOffice took too long to open that file.")
    made = out_dir / (source.stem + ".pdf")
    if not made.exists():
        raise Refused("LibreOffice couldn't turn that file into a PDF. It may be damaged, or protected by a password.")
    return made


def save_pdf(name: str, output: str = "") -> dict:
    """A tagged, accessible PDF of a Word document or PowerPoint, beside it."""
    path = documents.resolve(name)
    if path.suffix.lower() not in PDF_FILTERS:
        raise Refused("FreeHand saves Word documents and PowerPoints as PDF.")
    with tempfile.TemporaryDirectory() as tmp:
        made = _to_pdf(path, Path(tmp))
        out = documents.output_for(path, "", ".pdf", name=output or path.stem)
        shutil.copyfile(made, out)
    issues = documents._check_pdf(out)
    return {"saved_as": documents._rel(out), "original": f"\"{documents._rel(path)}\" is unchanged.",
            "accessibility": [i["problem"] for i in issues] or ["Tagged, with a title and language."],
            **({"tip": "Set the title or language in the original (document_fix_accessibility), then save as "
                       "PDF again."} if issues else {})}


def _pages(pdf: Path, wanted: List[int], dpi: int = 70) -> List[tuple]:
    """(page number, PNG bytes) for the pages asked for."""
    if not shutil.which("pdftoppm"):
        raise Refused(NOT_INSTALLED)
    out = []
    with tempfile.TemporaryDirectory() as tmp:
        for n in wanted:
            prefix = Path(tmp) / f"p{n}"
            subprocess.run(["pdftoppm", "-png", "-r", str(dpi), "-f", str(n), "-l", str(n), "-singlefile",
                            str(pdf), str(prefix)], capture_output=True, timeout=60, check=False)
            png = prefix.with_suffix(".png")
            if png.exists():
                out.append((n, png.read_bytes()))
    return out


def _page_count(pdf: Path) -> int:
    from pypdf import PdfReader
    return len(PdfReader(str(pdf)).pages)


def _which(pages, count: int) -> List[int]:
    """Page numbers from "1-3, 7" or a list, else the first few."""
    if not pages:
        return list(range(1, min(count, MAX_LOOK_PAGES) + 1))
    wanted = []
    for part in (pages if isinstance(pages, list) else re.split(r"[,\s]+", str(pages))):
        part = str(part).strip()
        if not part:
            continue
        m = re.match(r"^(\d+)\s*-\s*(\d+)$", part)
        nums = range(int(m.group(1)), int(m.group(2)) + 1) if m else ([int(part)] if part.isdigit() else [])
        wanted += [n for n in nums if 1 <= n <= count and n not in wanted]
    return wanted[:MAX_LOOK_PAGES]


LOOK_PROMPT = """You check how pages of a document look, for a blind author who can't see them. For each
picture (each is one page or slide, numbered), say briefly and plainly anything that looks wrong: text cut
off, overlapping or running off the edge; text too small or too faint against its background; pictures
stretched, cropped badly, blurry or covering text; things out of line or out of place; a nearly empty page;
fonts or sizes that don't match the rest. Also say in one short sentence what the page looks like overall.
If a page looks fine, say so. Don't comment on the writing itself unless asked. Reply with JSON only:
{"pages": [{"page": 1, "looks": "one sentence on the overall look", "problems": ["..."]}]}"""


async def look(name: str, pages=None, question: str = "") -> dict:
    """Pictures of some pages or slides, judged by the AI model in one call."""
    from core import agent, ai_use
    path = documents.resolve(name)
    kind = path.suffix.lower()
    if kind not in (".docx", ".pptx", ".pdf"):
        raise Refused("FreeHand can look at Word documents, PowerPoints and PDFs.")
    if kind != ".pdf" and not available():
        raise Refused(NOT_INSTALLED)
    over = ai_use.refusal()
    if over:
        return {"error": over}
    import asyncio
    with tempfile.TemporaryDirectory() as tmp:
        pdf = path if kind == ".pdf" else await asyncio.to_thread(_to_pdf, path, Path(tmp))
        count = await asyncio.to_thread(_page_count, pdf)
        wanted = _which(pages, count)
        if not wanted:
            raise Refused(f"It only has {count} page" + ("" if count == 1 else "s") + ".")
        shots = await asyncio.to_thread(_pages, pdf, wanted)
    if not shots:
        raise Refused("FreeHand couldn't make pictures of those pages.")
    word = "slide" if kind == ".pptx" else "page"
    content = [{"type": "text", "text": LOOK_PROMPT + (f"\nThe author also asks: {question[:300]}" if question else "")
                + f"\nThese are {word}s {', '.join(str(n) for n, _ in shots)} of {count}."}]
    for n, png in shots:
        content.append({"type": "text", "text": f"{word.capitalize()} {n}:"})
        content.append({"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(png).decode()}})
    reply = await agent.call_llm([{"role": "user", "content": content}])
    if "error" in reply:
        return {"error": "Your AI service didn't answer, so FreeHand couldn't look. Try again in a moment."}
    u = reply.get("_usage") or {}
    ai_use.count({"calls": 1, "prompt_tokens": int(u.get("prompt_tokens") or 0),
                  "completion_tokens": int(u.get("completion_tokens") or 0)})
    text = agent.strip_reasoning(str(reply.get("content") or ""))
    if "NO PICTURE" in text.upper() or not text.strip():
        return {"error": "Your AI model couldn't see the pages. Choose a model that can see pictures."}
    found = re.search(r"\{.*\}", text, re.S)
    try:
        result = json.loads(found.group(0)) if found else {"pages": [], "said": text.strip()}
    except ValueError:
        result = {"said": text.strip()}
    result.update({"file": documents._rel(path), "looked_at": [n for n, _ in shots], f"{word}s_in_all": count,
                   "note": "An AI's view of the pages: check anything important with someone you trust."})
    return result
