"""Working on documents in the person's work folder (Nov 2026).

The person puts documents in one folder they share with FreeHand
(FREEHAND_WORK_DIR in .env; /host/work in Docker; settings "work_dir" or
Documents/FreeHand work outside Docker). FreeHand reads Word, PowerPoint,
PDF, Excel, Markdown and text files there, and saves every result as a
NEW file beside the original ("... (edited).docx"). It never changes or
deletes a file that's already there.

What it can do:
- list the folder, and outline a file (its headings, slides, comments,
  tracked changes, pictures and tables, with numbers to refer to);
- check accessibility (Word, PowerPoint, PDF) and fix what can be fixed:
  title and language, table header rows, alt text for pictures (written
  by the AI model, which looks at each picture), slide titles;
- edit Word and PowerPoint while keeping the formatting: find and
  replace, rewrite or restyle a paragraph, add or remove paragraphs,
  slide titles and speaker notes;
- clean: take out comments, accept tracked changes, and remove editor's
  notes in brackets ("[Editor: ...]", "[TK]", "[check this]");
- combine Word, PDF, Markdown, text and Excel files into one Word
  document, pictures and all;
- create a Word document or PowerPoint from Markdown, with real
  headings, lists and slide titles.

Only the owner uses the work folder. Large files are read in part.
"""
from __future__ import annotations

import copy
import io
import logging
import os
import re
from pathlib import Path
from typing import List, Optional

log = logging.getLogger("freehand.documents")

IN_DOCKER = Path("/host/work")
KINDS = {".docx": "Word", ".pptx": "PowerPoint", ".pdf": "PDF", ".xlsx": "Excel",
         ".md": "Markdown", ".markdown": "Markdown", ".txt": "text", ".csv": "CSV"}
MAX_TEXT = 60_000
MAX_PARAGRAPHS = 400
MAX_ALT_PICTURES = 20

# Editor's notes in square brackets: [Editor: ...], [Note to self ...], [TK],
# [TBC], [check this], [TODO ...], [FIXME], [Comment: ...], [???].
NOTE_WORDS = r"(?:editor|ed\.?|note|notes|n\.b\.?|nb|tk|tbc|tbd|todo|to do|fixme|check|comment|query|q|\?+|xx+|placeholder|insert)"
BRACKET_NOTE = re.compile(r"\s*\[\s*" + NOTE_WORDS + r"\b[^\[\]]*\]|\s*\[\s*\?+\s*\]", re.I)

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
WP = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"


def _w(tag: str) -> str:
    return f"{{{W}}}{tag}"


# ── The folder ──────────────────────────────────────────────────────────

def folder() -> Optional[Path]:
    """The person's work folder, or None when FreeHand wasn't given one."""
    if IN_DOCKER.exists():
        shared = os.environ.get("FREEHAND_WORK_FROM", "").strip()
        return IN_DOCKER if shared and IN_DOCKER.is_dir() else None
    try:
        from core import agent_config
        named = str(agent_config._load_settings().get("work_dir") or "")
    except Exception:
        named = ""
    path = Path(named).expanduser() if named else Path.home() / "Documents" / "FreeHand work"
    return path if path.is_dir() else None


NO_FOLDER = ("FreeHand can't see a work folder yet. In FreeHand's .env, set FREEHAND_WORK_DIR to the "
             "folder you'd like it to work in (for example Documents\\FreeHand work), then restart FreeHand.")


class Refused(Exception):
    """A plain-words reason a request can't be done."""


def _owner() -> None:
    from core import people
    if people.current() != people.OWNER_ID:
        raise Refused("The work folder is only for FreeHand's owner.")


def resolve(name: str) -> Path:
    """A file in the work folder, by its name (or a path inside the folder).
    Never anywhere else."""
    _owner()
    place = folder()
    if not place:
        raise Refused(NO_FOLDER)
    name = str(name or "").strip().strip('"')
    if not name:
        raise Refused("Say which file, by its name.")
    candidate = Path(name)
    if not candidate.is_absolute():
        candidate = place / candidate
    try:
        candidate = candidate.resolve()
        candidate.relative_to(place.resolve())
    except (ValueError, OSError):
        raise Refused("FreeHand only works on files in your work folder.")
    if candidate.exists():
        return candidate
    # Not exact: one file whose name contains it.
    want = Path(name).name.lower()
    matches = [p for p in place.rglob("*") if p.is_file() and want in p.name.lower()]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise Refused("More than one file matches \"" + name + "\": " +
                      ", ".join(sorted(p.name for p in matches)[:10]) + ". Say which.")
    raise Refused(f"There's no \"{name}\" in your work folder.")


def _rel(path: Path) -> str:
    place = folder()
    try:
        return str(path.relative_to(place)) if place else path.name
    except ValueError:
        return path.name


def new_path(base: Path) -> Path:
    """base, or base (2), (3)... so nothing already there is overwritten."""
    if not base.exists():
        return base
    n = 2
    while True:
        candidate = base.with_name(f"{base.stem} ({n}){base.suffix}")
        if not candidate.exists():
            return candidate
        n += 1


def output_for(source: Path, label: str, suffix: Optional[str] = None, name: str = "") -> Path:
    """Where a result goes: the name asked for, or "<source> (label)", beside
    the source, never over an existing file, and always inside the folder."""
    suffix = suffix or source.suffix
    if name:
        clean = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "-", Path(name).name).strip(" .") or "result"
        if not clean.lower().endswith(suffix):
            clean += suffix
        return new_path(source.parent / clean)
    return new_path(source.with_name(f"{source.stem} ({label}){suffix}"))


def list_files() -> dict:
    _owner()
    place = folder()
    if not place:
        raise Refused(NO_FOLDER)
    files = []
    for p in sorted(place.rglob("*")):
        if p.is_file() and p.suffix.lower() in KINDS and not p.name.startswith("~$"):
            files.append({"name": _rel(p), "kind": KINDS[p.suffix.lower()],
                          "size_kb": round(p.stat().st_size / 1024)})
    return {"folder": "your work folder", "files": files[:300],
            "note": "FreeHand saves every result as a new file here, and never changes these."}


# ── Reading ─────────────────────────────────────────────────────────────

def _heading_level(style_name: str) -> Optional[int]:
    if style_name == "Title":
        return 0
    m = re.match(r"Heading (\d)", style_name or "")
    return int(m.group(1)) if m else None


# Alt text that's really a file name or a placeholder ("image.png",
# "Picture 3"): a screen reader reads it out, and it says nothing.
FAKE_ALT = re.compile(r"^\s*(?:[\w\- ]+\.(?:png|jpe?g|gif|bmp|tiff?|emf|wmf|svg|webp)|(?:image|picture|photo|graphic|img)\s*\d*)\s*$", re.I)


def _no_alt(text: str) -> bool:
    return not (text or "").strip() or bool(FAKE_ALT.match(text))


def _docx_pictures(doc) -> List[dict]:
    """Each picture in a Word document: its number, alt text, and where."""
    out = []
    for i, docpr in enumerate(doc.element.body.iter(f"{{{WP}}}docPr"), 1):
        out.append({"picture": i, "alt_text": docpr.get("descr", "") or "",
                    "decorative": "decorative" in (docpr.get("descr", "") or "").lower()})
    return out


def _tracked(body) -> dict:
    return {"insertions": len(list(body.iter(_w("ins")))), "deletions": len(list(body.iter(_w("del"))))}


def _comment_count(doc) -> int:
    return len(list(doc.element.body.iter(_w("commentReference"))))


def outline(name: str) -> dict:
    """A file's structure, with numbers to refer to when editing."""
    path = resolve(name)
    kind = path.suffix.lower()
    if kind == ".docx":
        return _docx_outline(path)
    if kind == ".pptx":
        return _pptx_outline(path)
    if kind == ".pdf":
        return _pdf_read(path)
    if kind == ".xlsx":
        return _xlsx_read(path)
    if kind in (".md", ".markdown", ".txt", ".csv"):
        text = path.read_text(encoding="utf-8", errors="replace")
        return {"file": _rel(path), "kind": KINDS[kind], "text": text[:MAX_TEXT],
                **({"cut_short": True} if len(text) > MAX_TEXT else {})}
    raise Refused(f"FreeHand can't read {path.suffix} files.")


def _docx_outline(path: Path) -> dict:
    from docx import Document
    doc = Document(str(path))
    paragraphs = []
    for i, p in enumerate(doc.paragraphs):
        if i >= MAX_PARAGRAPHS:
            break
        text = p.text
        if not text.strip():
            continue
        item = {"p": i, "text": text[:400]}
        level = _heading_level(p.style.name if p.style is not None else "")
        if level is not None:
            item["heading"] = level
        elif p.style is not None and p.style.name not in ("Normal", "Body Text"):
            item["style"] = p.style.name
        paragraphs.append(item)
    tables = [{"table": i + 1, "rows": len(t.rows), "columns": len(t.columns),
               "header_row": _has_header_row(t),
               "first_row": [c.text[:60] for c in t.rows[0].cells] if t.rows else []}
              for i, t in enumerate(doc.tables)]
    return {"file": _rel(path), "kind": "Word", "title": doc.core_properties.title or "",
            "language": doc.core_properties.language or "", "paragraphs": paragraphs,
            "paragraph_count": len(doc.paragraphs), "tables": tables,
            "pictures": _docx_pictures(doc), "comments": _comment_count(doc),
            "tracked_changes": _tracked(doc.element.body),
            **({"cut_short": f"Only the first {MAX_PARAGRAPHS} paragraphs."} if len(doc.paragraphs) > MAX_PARAGRAPHS else {})}


def _has_header_row(table) -> bool:
    if not table.rows:
        return False
    tr_pr = table.rows[0]._tr.trPr
    return tr_pr is not None and tr_pr.find(_w("tblHeader")) is not None


def _shape_alt(shape) -> str:
    try:
        return shape._element._nvXxPr.cNvPr.get("descr", "") or ""
    except AttributeError:
        return ""


def _slide_title(slide) -> str:
    try:
        t = slide.shapes.title
        return t.text_frame.text.strip() if t is not None and t.has_text_frame else ""
    except (AttributeError, KeyError):
        return ""


def _pptx_outline(path: Path) -> dict:
    from pptx import Presentation
    from pptx.enum.shapes import MSO_SHAPE_TYPE
    prs = Presentation(str(path))
    slides = []
    for n, slide in enumerate(prs.slides, 1):
        texts, pictures = [], []
        for shape in slide.shapes:
            if shape.shape_type == MSO_SHAPE_TYPE.PICTURE:
                pictures.append({"alt_text": _shape_alt(shape)})
            elif shape.has_text_frame and shape.text_frame.text.strip() and shape != slide.shapes.title:
                texts.append(shape.text_frame.text.strip()[:400])
        notes = slide.notes_slide.notes_text_frame.text.strip() if slide.has_notes_slide else ""
        slides.append({"slide": n, "title": _slide_title(slide), "text": texts, "pictures": pictures,
                       **({"speaker_notes": notes[:400]} if notes else {})})
    props = prs.core_properties
    return {"file": _rel(path), "kind": "PowerPoint", "title": props.title or "",
            "language": props.language or "", "slides": slides}


def _pdf_read(path: Path) -> dict:
    try:
        from pypdf import PdfReader
    except ImportError:
        raise Refused("PDF reading isn't installed in this FreeHand yet. Updating FreeHand should fix it.")
    reader = PdfReader(str(path))
    pages, total = [], 0
    for i, page in enumerate(reader.pages, 1):
        try:
            text = page.extract_text() or ""
        except Exception:
            text = ""
        if total < MAX_TEXT:
            pages.append({"page": i, "text": text[: MAX_TEXT - total]})
        total += len(text)
    meta = reader.metadata or {}
    tagged, language = False, ""
    try:
        root = reader.trailer["/Root"]
        if "/MarkInfo" in root:
            tagged = bool(root["/MarkInfo"].get("/Marked"))
        tagged = tagged or "/StructTreeRoot" in root
        language = str(root["/Lang"]) if "/Lang" in root else ""
    except Exception:
        pass
    return {"file": _rel(path), "kind": "PDF", "pages": pages, "page_count": len(reader.pages),
            "title": str(meta.get("/Title") or ""), "language": language,
            "tagged": tagged, "has_text": total > 0,
            **({"cut_short": True} if total > MAX_TEXT else {})}


def _xlsx_read(path: Path) -> dict:
    from openpyxl import load_workbook
    wb = load_workbook(str(path), read_only=True, data_only=True)
    sheets, cells = [], 0
    for ws in wb.worksheets:
        rows = []
        for row in ws.iter_rows(values_only=True):
            if cells > 5000:
                break
            rows.append(["" if v is None else str(v) for v in row])
            cells += len(row)
        while rows and not any(rows[-1]):
            rows.pop()
        sheets.append({"sheet": ws.title, "rows": rows[:200]})
    return {"file": _rel(path), "kind": "Excel", "sheets": sheets}


# ── Checking accessibility ──────────────────────────────────────────────

VAGUE_LINKS = {"click here", "here", "read more", "more", "link", "this link", "this"}


def check(name: str) -> dict:
    """Accessibility problems, most important first, each with how to fix it."""
    path = resolve(name)
    kind = path.suffix.lower()
    if kind == ".docx":
        issues = _check_docx(path)
    elif kind == ".pptx":
        issues = _check_pptx(path)
    elif kind == ".pdf":
        issues = _check_pdf(path)
    else:
        raise Refused("FreeHand checks the accessibility of Word, PowerPoint and PDF files.")
    fixable = sum(1 for i in issues if i.get("fix_tool"))
    return {"file": _rel(path), "problems": issues, "count": len(issues),
            "summary": ("No problems found." if not issues else
                        f"{len(issues)} problem" + ("" if len(issues) == 1 else "s") +
                        f", {fixable} of which document_fix_accessibility can fix.")}


def _check_docx(path: Path) -> List[dict]:
    from docx import Document
    doc = Document(str(path))
    issues = []
    pictures = _docx_pictures(doc)
    missing_alt = [p["picture"] for p in pictures if _no_alt(p["alt_text"])]
    if missing_alt:
        issues.append({"problem": f"{len(missing_alt)} picture(s) have no alt text (pictures {missing_alt[:15]}).",
                       "why": "A screen reader says only \"picture\", so the person misses what it shows.",
                       "fix_tool": "document_fix_accessibility (writes alt text by looking at each picture)"})
    levels = []
    fake = []
    for i, p in enumerate(doc.paragraphs):
        level = _heading_level(p.style.name if p.style is not None else "")
        if level:
            levels.append((i, level))
        elif p.text.strip() and len(p.text) < 80 and p.runs and all(r.bold for r in p.runs if r.text.strip()):
            fake.append(i)
    if not levels:
        issues.append({"problem": "There are no headings.",
                       "why": "Screen reader users move through a document by its headings; without them it's one long page.",
                       "fix": "Give the section titles a Heading style (document_edit with style)."})
    elif levels[0][1] != 1:
        issues.append({"problem": f"The first heading is level {levels[0][1]}, not level 1 (paragraph {levels[0][0]}).",
                       "why": "Headings should start at level 1 so the outline makes sense.",
                       "fix": "Change it to Heading 1 (document_edit with style)."})
    skips = [(i, a, b) for (_, a), (i, b) in zip(levels, levels[1:]) if b > a + 1]
    if skips:
        i, a, b = skips[0]
        issues.append({"problem": f"Heading levels skip, e.g. level {a} to level {b} at paragraph {i} "
                                  f"({len(skips)} place(s)).",
                       "why": "A skipped level makes it look as if part of the document is missing.",
                       "fix": "Use the next level down (document_edit with style)."})
    if fake:
        issues.append({"problem": f"{len(fake)} short, all-bold paragraph(s) look like headings but aren't "
                                  f"(paragraphs {fake[:10]}).",
                       "why": "They look like headings but a screen reader can't jump to them.",
                       "fix": "If they're section titles, give them a Heading style (document_edit with style)."})
    no_header = [i + 1 for i, t in enumerate(doc.tables) if len(t.rows) > 1 and not _has_header_row(t)]
    if no_header:
        issues.append({"problem": f"{len(no_header)} table(s) have no header row (tables {no_header}).",
                       "why": "A screen reader can't say which column each cell belongs to.",
                       "fix_tool": "document_fix_accessibility (marks the first row as the header row)"})
    if not (doc.core_properties.title or "").strip():
        issues.append({"problem": "The document has no title in its properties.",
                       "why": "Screen readers and PDF readers announce the title; without it they read the file name.",
                       "fix_tool": "document_fix_accessibility"})
    if not (doc.core_properties.language or "").strip() and not _docx_default_lang(doc):
        issues.append({"problem": "The document's language isn't set.",
                       "why": "Screen readers use it to choose the right voice and pronunciation.",
                       "fix_tool": "document_fix_accessibility"})
    empties, run, worst = 0, 0, 0
    for p in doc.paragraphs:
        run = run + 1 if not p.text.strip() and not list(p._p.iter(f"{{{WP}}}docPr")) else 0
        worst = max(worst, run)
        empties += 1 if run == 3 else 0
    if empties:
        issues.append({"problem": f"Empty paragraphs are used for spacing ({empties} place(s) with 3 or more in a row).",
                       "why": "A screen reader says \"blank\" for each one.",
                       "fix": "Remove them (document_edit with delete) and use spacing before or after paragraphs instead."})
    vague = []
    for link in doc.element.body.iter(_w("hyperlink")):
        text = "".join(t.text or "" for t in link.iter(_w("t"))).strip()
        if text.lower() in VAGUE_LINKS or re.match(r"https?://", text):
            vague.append(text)
    if vague:
        issues.append({"problem": f"{len(vague)} link(s) don't say where they go (e.g. \"{vague[0][:50]}\").",
                       "why": "Screen reader users often list the links on a page; \"click here\" tells them nothing.",
                       "fix": "Change the link text to say where it goes (document_edit with find and replace)."})
    if _comment_count(doc) or any(_tracked(doc.element.body).values()):
        issues.append({"problem": "There are comments or tracked changes left in.",
                       "why": "They're read out and can confuse, and they may not be meant for readers.",
                       "fix_tool": "document_clean"})
    return issues


def _docx_default_lang(doc) -> str:
    from docx.oxml.ns import qn
    defaults = doc.styles.element.find(qn("w:docDefaults"))
    if defaults is None:
        return ""
    lang = defaults.find(qn("w:rPrDefault") + "/" + qn("w:rPr") + "/" + qn("w:lang"))
    return lang.get(qn("w:val"), "") if lang is not None else ""


def _check_pptx(path: Path) -> List[dict]:
    from pptx import Presentation
    from pptx.enum.shapes import MSO_SHAPE_TYPE
    prs = Presentation(str(path))
    issues = []
    untitled, alt_missing, titles, small = [], [], {}, []
    for n, slide in enumerate(prs.slides, 1):
        title = _slide_title(slide)
        if not title:
            untitled.append(n)
        else:
            titles.setdefault(title.lower(), []).append(n)
        for shape in slide.shapes:
            if shape.shape_type == MSO_SHAPE_TYPE.PICTURE and _no_alt(_shape_alt(shape)):
                alt_missing.append(n)
            if shape.has_text_frame:
                for para in shape.text_frame.paragraphs:
                    for r in para.runs:
                        if r.font.size is not None and r.font.size.pt < 18 and r.text.strip():
                            small.append(n)
    if untitled:
        issues.append({"problem": f"{len(untitled)} slide(s) have no title (slides {untitled[:15]}).",
                       "why": "Screen reader users find slides by their titles; an untitled slide is just \"slide 4\".",
                       "fix": "Give each a title (document_edit with slide and title)."})
    repeated = {t: s for t, s in titles.items() if len(s) > 1}
    if repeated:
        t, s = next(iter(repeated.items()))
        issues.append({"problem": f"Some slides share a title (e.g. slides {s} are all \"{t}\").",
                       "why": "Identical titles make slides hard to tell apart in a list.",
                       "fix": "Add a word to tell them apart, e.g. \"(continued)\" or \"2 of 3\"."})
    if alt_missing:
        issues.append({"problem": f"{len(alt_missing)} picture(s) have no alt text, or only a file name "
                                  f"(on slides {sorted(set(alt_missing))[:15]}).",
                       "why": "A screen reader says only \"picture\".",
                       "fix_tool": "document_fix_accessibility (writes alt text by looking at each picture)"})
    if small:
        issues.append({"problem": f"Some text is smaller than 18 point (slides {sorted(set(small))[:15]}).",
                       "why": "Small text is hard to read on a projector or for people with low vision.",
                       "fix": "Make it bigger, or move detail into the speaker notes."})
    if not (prs.core_properties.title or "").strip():
        issues.append({"problem": "The presentation has no title in its properties.",
                       "why": "It's what's announced when the file opens.",
                       "fix_tool": "document_fix_accessibility"})
    if not (prs.core_properties.language or "").strip():
        issues.append({"problem": "The presentation's language isn't set.",
                       "why": "Screen readers use it to choose the right voice and pronunciation.",
                       "fix_tool": "document_fix_accessibility"})
    return issues


def _check_pdf(path: Path) -> List[dict]:
    info = _pdf_read(path)
    issues = []
    if not info["has_text"]:
        issues.append({"problem": "The PDF has no text: it's pictures of pages (probably scanned).",
                       "why": "A screen reader can't read it at all.",
                       "fix": "Ask for the original document, or have the PDF run through text recognition (OCR)."})
    if not info["tagged"]:
        issues.append({"problem": "The PDF isn't tagged.",
                       "why": "Without tags, a screen reader has no headings, lists or table structure, and may read in the wrong order.",
                       "fix": "Make it again from an accessible Word document (Save as PDF, with document structure tags on)."})
    if not info["title"].strip():
        issues.append({"problem": "The PDF has no title.", "why": "The file name is read out instead.",
                       "fix": "Set the title in the original document's properties before saving as PDF."})
    if not info["language"].strip():
        issues.append({"problem": "The PDF's language isn't set.",
                       "why": "Screen readers use it to choose the right voice and pronunciation.",
                       "fix": "Set the language in the original document before saving as PDF."})
    return issues


# ── Word: editing while keeping the formatting ──────────────────────────

def _replace_in_paragraph(p, find: str, replace: str) -> int:
    """Replace text that may be split across runs, keeping each run's
    formatting (the replacement takes the first run's)."""
    count, start_at = 0, 0
    while True:
        runs = p.runs
        full = "".join(r.text for r in runs)
        at = full.find(find, start_at) if find else -1
        if at < 0:
            return count
        end = at + len(find)
        start_at = at + len(replace)  # never search the replacement itself
        pos, first = 0, True
        for r in runs:
            start, stop = pos, pos + len(r.text)
            pos = stop
            if stop <= at or start >= end:
                continue
            keep_before = r.text[: max(0, at - start)]
            keep_after = r.text[max(0, end - start):] if stop > end else ""
            if first:
                r.text = keep_before + replace + keep_after
                first = False
            else:
                r.text = keep_after
        count += 1
        if count > 1000:
            return count


def _all_paragraphs(doc):
    """Body paragraphs, then those in tables, headers and footers."""
    yield from doc.paragraphs
    for t in doc.tables:
        for row in t.rows:
            for cell in row.cells:
                yield from cell.paragraphs
    for section in doc.sections:
        for part in (section.header, section.footer):
            yield from part.paragraphs


def edit_docx(name: str, changes: List[dict], output: str = "") -> dict:
    """Apply changes, in order, to a copy. Each change is one of:
    {"find", "replace"}                  everywhere, formatting kept
    {"paragraph": n, "text": "..."}      rewrite paragraph n (first run's formatting)
    {"paragraph": n, "style": "Heading 2"}
    {"paragraph": n, "insert_after": "...", "style": optional}
    {"paragraph": n, "delete": true}
    Paragraph numbers are the "p" numbers from document_outline."""
    from docx import Document
    path = resolve(name)
    if path.suffix.lower() != ".docx":
        raise Refused("document_edit for Word works on .docx files.")
    doc = Document(str(path))
    paragraphs = list(doc.paragraphs)
    done, skipped, to_delete = [], [], []
    for c in changes or []:
        if not isinstance(c, dict):
            continue
        if "find" in c:
            n = sum(_replace_in_paragraph(p, str(c["find"]), str(c.get("replace", ""))) for p in _all_paragraphs(doc))
            (done if n else skipped).append(f"\"{str(c['find'])[:40]}\": {n} replaced" if n else
                                            f"\"{str(c['find'])[:40]}\" wasn't found")
            continue
        try:
            n = int(c.get("paragraph"))
            p = paragraphs[n]
        except (TypeError, ValueError, IndexError):
            skipped.append(f"paragraph {c.get('paragraph')} doesn't exist")
            continue
        if c.get("delete"):
            to_delete.append(p)
            done.append(f"paragraph {n} removed")
        if "text" in c:
            runs = p.runs
            if runs:
                runs[0].text = str(c["text"])
                for r in runs[1:]:
                    r._r.getparent().remove(r._r)
            else:
                p.add_run(str(c["text"]))
            done.append(f"paragraph {n} rewritten")
        if c.get("style") and "insert_after" not in c:
            try:
                p.style = doc.styles[str(c["style"])]
                done.append(f"paragraph {n} is now {c['style']}")
            except KeyError:
                skipped.append(f"there's no style called {c['style']}")
        if "insert_after" in c:
            new = copy.deepcopy(p._p)
            for child in list(new):
                if child.tag != _w("pPr"):
                    new.remove(child)
            p._p.addnext(new)
            from docx.text.paragraph import Paragraph
            added = Paragraph(new, p._parent)
            added.add_run(str(c["insert_after"]))
            if c.get("style"):
                try:
                    added.style = doc.styles[str(c["style"])]
                except KeyError:
                    skipped.append(f"there's no style called {c['style']}")
            done.append(f"a paragraph added after {n}")
    for p in to_delete:
        p._p.getparent().remove(p._p)
    if not done:
        return {"error": "Nothing was changed: " + "; ".join(skipped or ["no changes were given"])}
    out = output_for(path, "edited", name=output)
    doc.save(str(out))
    return {"saved_as": _rel(out), "changes": done, **({"not_done": skipped} if skipped else {}),
            "original": f"\"{_rel(path)}\" is unchanged."}


# ── Cleaning: comments, tracked changes, editor's notes ────────────────

def _accept_tracked(body) -> int:
    n = 0
    for tag in ("del", "moveFrom"):
        for el in list(body.iter(_w(tag))):
            el.getparent().remove(el)
            n += 1
    for tag in ("ins", "moveTo"):
        for el in list(body.iter(_w(tag))):
            parent = el.getparent()
            i = parent.index(el)
            for child in list(el):
                parent.insert(i, child)
                i += 1
            parent.remove(el)
            n += 1
    for tag in ("rPrChange", "pPrChange", "sectPrChange", "tblPrChange", "trPrChange", "tcPrChange",
                "moveFromRangeStart", "moveFromRangeEnd", "moveToRangeStart", "moveToRangeEnd"):
        for el in list(body.iter(_w(tag))):
            el.getparent().remove(el)
    return n


def _drop_comments(doc) -> int:
    body = doc.element.body
    n = len(list(body.iter(_w("commentReference"))))
    for tag in ("commentRangeStart", "commentRangeEnd"):
        for el in list(body.iter(_w(tag))):
            el.getparent().remove(el)
    for ref in list(body.iter(_w("commentReference"))):
        run = ref.getparent()
        if run.tag == _w("r") and run.getparent() is not None:
            run.getparent().remove(run)
        else:
            run.remove(ref)
    for rel_id, rel in list(doc.part.rels.items()):
        if rel.reltype.endswith(("/comments", "/commentsExtended", "/commentsIds", "/commentsExtensible")):
            doc.part.drop_rel(rel_id)
    return n


def _strip_notes(paragraphs, also: List[str]) -> List[str]:
    removed = []
    extra = [re.compile(re.escape(a), re.I) for a in also if a]
    for p in paragraphs:
        text = p.text
        if not text:
            continue
        found = [m.group(0).strip() for m in BRACKET_NOTE.finditer(text)]
        for pat in extra:
            found += [m.group(0) for m in pat.finditer(text)]
        for snippet in found:
            if _replace_in_paragraph(p, snippet, ""):
                removed.append(snippet[:120])
        # A space left before punctuation, or doubled, by a removal.
        if found:
            for r in p.runs:
                r.text = re.sub(r" {2,}", " ", re.sub(r" +([,.;:!?])", r"\1", r.text))
    return removed


def clean(name: str, comments: bool = True, tracked: bool = True, notes: bool = True,
          also_remove: Optional[List[str]] = None, speaker_notes: bool = False, output: str = "") -> dict:
    """A copy without comments, with tracked changes accepted, and without
    editor's notes in brackets (and any exact phrases in also_remove)."""
    path = resolve(name)
    also = [str(a) for a in (also_remove or []) if str(a).strip()]
    report = {}
    if path.suffix.lower() == ".docx":
        from docx import Document
        doc = Document(str(path))
        if tracked:
            report["tracked_changes_accepted"] = _accept_tracked(doc.element.body)
        if comments:
            report["comments_removed"] = _drop_comments(doc)
        if notes or also:
            removed = _strip_notes(list(_all_paragraphs(doc)), also) if notes else \
                _strip_notes_only(list(_all_paragraphs(doc)), also)
            report["notes_removed"] = removed
    elif path.suffix.lower() == ".pptx":
        from pptx import Presentation
        prs = Presentation(str(path))
        removed = []
        for slide in prs.slides:
            for shape in slide.shapes:
                if shape.has_text_frame:
                    removed += _strip_notes(shape.text_frame.paragraphs, also) if notes else \
                        _strip_notes_only(shape.text_frame.paragraphs, also)
            if speaker_notes and slide.has_notes_slide:
                tf = slide.notes_slide.notes_text_frame
                if tf.text.strip():
                    removed.append(f"speaker notes: {tf.text.strip()[:80]}")
                    tf.text = ""
        if comments:
            n = 0
            for slide in prs.slides:
                for rel_id, rel in list(slide.part.rels.items()):
                    if rel.reltype.endswith("/comments"):
                        slide.part.drop_rel(rel_id)
                        n += 1
            report["slides_with_comments_cleared"] = n
        report["notes_removed"] = removed
        doc = prs
    else:
        raise Refused("document_clean works on Word and PowerPoint files.")
    changed = any(v for v in report.values())
    if not changed:
        return {"nothing_to_clean": True, "file": _rel(path),
                "note": "No comments, tracked changes or editor's notes were found, so no copy was made."}
    out = output_for(path, "clean", name=output)
    doc.save(str(out))
    return {"saved_as": _rel(out), **report, "original": f"\"{_rel(path)}\" is unchanged.",
            "check": "Read the list of removed notes, in case one was meant for readers."}


def _strip_notes_only(paragraphs, also: List[str]) -> List[str]:
    removed = []
    for p in paragraphs:
        for a in also:
            if a and a in p.text and _replace_in_paragraph(p, a, ""):
                removed.append(a[:120])
    return removed


# ── Combining into one Word document ────────────────────────────────────

def _copy_pictures(source_doc, target_doc, element) -> None:
    """Pictures in copied content point at the source's parts: add each one
    to the target and point the copy at it."""
    for blip in element.iter(f"{{{A}}}blip"):
        rid = blip.get(f"{{{R}}}embed")
        if not rid or rid not in source_doc.part.rels:
            continue
        image_part = source_doc.part.rels[rid].target_part
        new_rid, _ = target_doc.part.get_or_add_image(io.BytesIO(image_part.blob))
        blip.set(f"{{{R}}}embed", new_rid)


def _append_docx(target, source_path: Path) -> None:
    from docx import Document
    source = Document(str(source_path))
    body = target.element.body
    sect = body.find(_w("sectPr"))
    for el in list(source.element.body):
        if el.tag == _w("sectPr"):
            continue
        new = copy.deepcopy(el)
        _copy_pictures(source, target, new)
        # Hyperlinks and other relationships can't be carried over safely: keep the text.
        for link in list(new.iter(_w("hyperlink"))):
            link.attrib.pop(f"{{{R}}}id", None)
        if sect is not None:
            sect.addprevious(new)
        else:
            body.append(new)


def _add_markdown(doc, text: str) -> None:
    """Markdown into a Word document: headings, lists, bold, italic, tables."""
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].rstrip()
        if not line.strip():
            i += 1
            continue
        h = re.match(r"^(#{1,6})\s+(.*)", line)
        if h:
            doc.add_heading(h.group(2).strip(), level=min(len(h.group(1)), 6))
            i += 1
            continue
        if line.lstrip().startswith("|") and i + 1 < len(lines) and re.match(r"^\s*\|?\s*:?-{3,}", lines[i + 1]):
            rows = []
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                if not re.match(r"^\s*\|?\s*:?-{3,}", lines[i]):
                    rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            if rows:
                width = max(len(r) for r in rows)
                t = doc.add_table(rows=0, cols=width)
                t.style = "Table Grid"
                for r in rows:
                    cells = t.add_row().cells
                    for c, v in zip(cells, r):
                        _md_runs(c.paragraphs[0], v)
                _mark_header_row(t)
            continue
        bullet = re.match(r"^\s*[-*+]\s+(.*)", line)
        number = re.match(r"^\s*\d+[.)]\s+(.*)", line)
        if bullet or number:
            p = doc.add_paragraph(style="List Bullet" if bullet else "List Number")
            _md_runs(p, (bullet or number).group(1))
            i += 1
            continue
        para = [line.strip()]
        i += 1
        while i < len(lines) and lines[i].strip() and not re.match(r"^(#{1,6}\s|\s*[-*+]\s|\s*\d+[.)]\s|\s*\|)", lines[i]):
            para.append(lines[i].strip())
            i += 1
        _md_runs(doc.add_paragraph(), " ".join(para))


def _md_runs(p, text: str) -> None:
    for part in re.split(r"(\*\*[^*]+\*\*|\*[^*]+\*|_[^_]+_)", text):
        if not part:
            continue
        if part.startswith("**") and part.endswith("**"):
            p.add_run(part[2:-2]).bold = True
        elif (part.startswith("*") and part.endswith("*")) or (part.startswith("_") and part.endswith("_")):
            p.add_run(part[1:-1]).italic = True
        else:
            p.add_run(re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 (\2)", part))


def _mark_header_row(table) -> None:
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    if not table.rows:
        return
    tr_pr = table.rows[0]._tr.get_or_add_trPr()
    if tr_pr.find(qn("w:tblHeader")) is None:
        head = OxmlElement("w:tblHeader")
        head.set(qn("w:val"), "true")
        tr_pr.append(head)


def _set_language(doc, code: str) -> None:
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    doc.core_properties.language = code
    defaults = doc.styles.element.find(qn("w:docDefaults"))
    if defaults is None:
        return
    rpr = defaults.find(qn("w:rPrDefault") + "/" + qn("w:rPr"))
    if rpr is None:
        return
    lang = rpr.find(qn("w:lang"))
    if lang is None:
        lang = OxmlElement("w:lang")
        rpr.append(lang)
    lang.set(qn("w:val"), code)


def combine(names: List[str], output: str, title: str = "", headings: bool = False,
            page_breaks: bool = True) -> dict:
    """Join files into one Word document, in the order given. Word files
    keep their formatting and pictures; PDF, Markdown, text and Excel come
    in as text, headings and tables."""
    from docx import Document
    from docx.enum.text import WD_BREAK
    if not names or len(names) < 2:
        raise Refused("Name at least two files to combine, in the order you want them.")
    paths = [resolve(n) for n in names]
    if not output:
        raise Refused("Say what to call the combined document.")
    first_word = next((p for p in paths if p.suffix.lower() == ".docx"), None)
    doc = Document(str(first_word)) if first_word is paths[0] else Document()
    if first_word is not paths[0]:
        _set_language(doc, "en-ZA")
    for i, p in enumerate(paths):
        if i == 0 and first_word is paths[0]:
            if headings:
                doc.paragraphs[0].insert_paragraph_before(p.stem, style="Heading 1") if doc.paragraphs else \
                    doc.add_heading(p.stem, level=1)
            continue
        if i > 0 and page_breaks:
            doc.add_paragraph().add_run().add_break(WD_BREAK.PAGE)
        if headings:
            doc.add_heading(p.stem, level=1)
        kind = p.suffix.lower()
        if kind == ".docx":
            _append_docx(doc, p)
        elif kind in (".md", ".markdown"):
            _add_markdown(doc, p.read_text(encoding="utf-8", errors="replace"))
        elif kind in (".txt", ".csv"):
            for block in re.split(r"\n\s*\n", p.read_text(encoding="utf-8", errors="replace")):
                if block.strip():
                    doc.add_paragraph(" ".join(block.split()))
        elif kind == ".pdf":
            for page in _pdf_read(p)["pages"]:
                for block in re.split(r"\n\s*\n", page["text"]):
                    if block.strip():
                        doc.add_paragraph(" ".join(block.split()))
        elif kind == ".xlsx":
            for sheet in _xlsx_read(p)["sheets"]:
                rows = [r for r in sheet["rows"] if any(r)]
                if not rows:
                    continue
                doc.add_heading(sheet["sheet"], level=2)
                t = doc.add_table(rows=0, cols=max(len(r) for r in rows))
                t.style = "Table Grid"
                for r in rows:
                    for c, v in zip(t.add_row().cells, r):
                        c.text = v
                _mark_header_row(t)
        else:
            raise Refused(f"FreeHand can't combine {p.suffix} files.")
    if title:
        doc.core_properties.title = title
    out = output_for(paths[0], "combined", ".docx", name=output)
    doc.save(str(out))
    return {"saved_as": _rel(out), "from": [_rel(p) for p in paths],
            "note": "Word files keep their formatting and pictures. Links inside them keep their text but not "
                    "their web address, and PDF text may need tidying. The originals are unchanged."}


# ── Creating from Markdown ──────────────────────────────────────────────

def create(output: str, markdown: str, kind: str = "word", title: str = "", language: str = "en-ZA") -> dict:
    """A new Word document or PowerPoint from Markdown.
    Word: # headings, lists, **bold**, *italic*, tables.
    PowerPoint: each "# " heading starts a slide (its title); "- " lines
    are bullets; lines after "Notes:" are speaker notes."""
    _owner()
    place = folder()
    if not place:
        raise Refused(NO_FOLDER)
    if not (markdown or "").strip():
        raise Refused("There's nothing to put in the document.")
    if kind.lower() in ("powerpoint", "pptx", "slides", "presentation"):
        out = output_for(place / "x.pptx", "", ".pptx", name=output or "presentation")
        prs = _slides_from_markdown(markdown, title)
        prs.core_properties.title = title or _first_heading(markdown) or out.stem
        prs.core_properties.language = language
        prs.save(str(out))
        return {"saved_as": _rel(out), "slides": len(prs.slides)}
    from docx import Document
    doc = Document()
    _set_language(doc, language)
    doc.core_properties.title = title or _first_heading(markdown) or Path(output or "document").stem
    if title and not markdown.lstrip().startswith("#"):
        doc.add_heading(title, level=0)
    _add_markdown(doc, markdown)
    out = output_for(place / "x.docx", "", ".docx", name=output or "document")
    doc.save(str(out))
    return {"saved_as": _rel(out)}


def _first_heading(markdown: str) -> str:
    m = re.search(r"^#{1,6}\s+(.+)$", markdown, re.M)
    return m.group(1).strip() if m else ""


def _slides_from_markdown(markdown: str, title: str = ""):
    from pptx import Presentation
    from pptx.util import Inches
    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)  # 16:9
    title_layout, content_layout = prs.slide_layouts[0], prs.slide_layouts[1]
    blocks = re.split(r"^#{1,6}\s+", markdown, flags=re.M)
    lead = blocks[0].strip()
    if title or lead:
        s = prs.slides.add_slide(title_layout)
        s.shapes.title.text = title or lead.splitlines()[0]
        if len(s.placeholders) > 1 and lead and title:
            s.placeholders[1].text = lead.splitlines()[0]
    for block in blocks[1:]:
        lines = block.splitlines()
        heading = lines[0].strip()
        bullets, notes, in_notes = [], [], False
        for line in lines[1:]:
            if re.match(r"^\s*notes?:", line, re.I):
                in_notes = True
                rest = line.split(":", 1)[1].strip()
                if rest:
                    notes.append(rest)
                continue
            if not line.strip():
                continue
            (notes if in_notes else bullets).append(re.sub(r"^\s*(?:[-*+]|\d+[.)])\s+", "", line).strip())
        s = prs.slides.add_slide(content_layout)
        s.shapes.title.text = heading
        body = s.placeholders[1].text_frame
        body.text = bullets[0] if bullets else ""
        for b in bullets[1:]:
            body.add_paragraph().text = b
        if not bullets:
            s.placeholders[1]._element.getparent().remove(s.placeholders[1]._element)
        if notes:
            s.notes_slide.notes_text_frame.text = "\n".join(notes)
    return prs


# ── PowerPoint: editing ─────────────────────────────────────────────────

def edit_pptx(name: str, changes: List[dict], output: str = "") -> dict:
    """Changes, in order, to a copy. Each one is:
    {"find", "replace"}                    on every slide, formatting kept
    {"slide": n, "title": "..."}            set or add slide n's title
    {"slide": n, "notes": "..."}            replace its speaker notes
    {"slide": n, "picture": k, "alt_text": "..."}   alt text for its k-th picture"""
    from pptx import Presentation
    from pptx.enum.shapes import MSO_SHAPE_TYPE
    path = resolve(name)
    if path.suffix.lower() != ".pptx":
        raise Refused("document_edit for PowerPoint works on .pptx files.")
    prs = Presentation(str(path))
    slides = list(prs.slides)
    done, skipped = [], []
    for c in changes or []:
        if not isinstance(c, dict):
            continue
        if "find" in c:
            n = 0
            for slide in slides:
                for shape in slide.shapes:
                    if shape.has_text_frame:
                        n += sum(_replace_in_paragraph(p, str(c["find"]), str(c.get("replace", "")))
                                 for p in shape.text_frame.paragraphs)
            (done if n else skipped).append(f"\"{str(c['find'])[:40]}\": {n} replaced" if n else
                                            f"\"{str(c['find'])[:40]}\" wasn't found")
            continue
        try:
            k = int(c.get("slide"))
            slide = slides[k - 1]
            if k < 1:
                raise IndexError
        except (TypeError, ValueError, IndexError):
            skipped.append(f"slide {c.get('slide')} doesn't exist")
            continue
        if "title" in c:
            if slide.shapes.title is not None:
                slide.shapes.title.text = str(c["title"])
            else:
                from pptx.util import Inches, Pt
                box = slide.shapes.add_textbox(Inches(0.5), Inches(0.2), prs.slide_width - Inches(1), Inches(1))
                box.text_frame.text = str(c["title"])
                box.text_frame.paragraphs[0].runs[0].font.size = Pt(36)
                box.name = "Title"
                # First in reading order, as a title should be.
                tree = slide.shapes._spTree
                tree.remove(box._element)
                tree.insert(2, box._element)
                skipped.append(f"slide {k} has no title box, so its title was added as text at the top: "
                               "choose a layout with a title in PowerPoint for the best result")
            done.append(f"slide {k} title set")
        if "notes" in c:
            slide.notes_slide.notes_text_frame.text = str(c["notes"])
            done.append(f"slide {k} speaker notes set")
        if "alt_text" in c:
            pics = [s for s in slide.shapes if s.shape_type == MSO_SHAPE_TYPE.PICTURE]
            try:
                pic = pics[int(c.get("picture", 1)) - 1]
                pic._element._nvXxPr.cNvPr.set("descr", str(c["alt_text"]))
                done.append(f"slide {k} picture {c.get('picture', 1)} alt text set")
            except (IndexError, ValueError, TypeError):
                skipped.append(f"slide {k} has no picture {c.get('picture', 1)}")
    if not done:
        return {"error": "Nothing was changed: " + "; ".join(skipped or ["no changes were given"])}
    out = output_for(path, "edited", name=output)
    prs.save(str(out))
    return {"saved_as": _rel(out), "changes": done, **({"not_done": skipped} if skipped else {}),
            "original": f"\"{_rel(path)}\" is unchanged."}


def edit(name: str, changes: List[dict], output: str = "") -> dict:
    path = resolve(name)
    if path.suffix.lower() == ".pptx":
        return edit_pptx(name, changes, output)
    return edit_docx(name, changes, output)


# ── Fixing accessibility ────────────────────────────────────────────────

ALT_PROMPT = (
    "Write alt text for this picture, which is in a document. One or two plain sentences saying what it "
    "shows and why it matters, as a screen reader user would want it; no \"image of\" or \"picture of\". "
    "If it has words in it, include them. If it's only decoration (a line, a border, a background), reply "
    "with exactly: DECORATIVE. If you can't see a picture, reply with exactly: NO PICTURE.")


async def describe(image: bytes, context: str = "") -> Optional[str]:
    """Alt text from the AI model, "" for decorative, or None if it couldn't."""
    import base64
    from core import agent, ai_use
    try:
        from PIL import Image
        pic = Image.open(io.BytesIO(image)).convert("RGB")
        pic.thumbnail((1024, 1024))
        buf = io.BytesIO()
        pic.save(buf, "JPEG", quality=85)
        image = buf.getvalue()
    except Exception:
        return None  # a format the model wouldn't take either (e.g. EMF)
    if ai_use.refusal():
        return None
    url = "data:image/jpeg;base64," + base64.b64encode(image).decode("ascii")
    content = [{"type": "text", "text": ALT_PROMPT + (f"\nThe text near it: {context[:500]}" if context else "")},
               {"type": "image_url", "image_url": {"url": url}}]
    reply = await agent.call_llm([{"role": "user", "content": content}])
    if "error" in reply:
        return None
    u = reply.get("_usage") or {}
    ai_use.count({"calls": 1, "prompt_tokens": int(u.get("prompt_tokens") or 0),
                  "completion_tokens": int(u.get("completion_tokens") or 0)})
    text = agent.strip_reasoning(str(reply.get("content") or "")).strip().strip('"')
    if not text or text.upper().startswith("NO PICTURE"):
        return None
    return "" if text.upper().startswith("DECORATIVE") else text[:500]


async def fix(name: str, title: str = "", language: str = "en-ZA", alt_text: bool = True,
              output: str = "") -> dict:
    """A copy with what can be fixed automatically: title, language, table
    header rows, and alt text for pictures that have none."""
    path = resolve(name)
    kind = path.suffix.lower()
    if kind not in (".docx", ".pptx"):
        raise Refused("document_fix_accessibility works on Word and PowerPoint files. For a PDF, fix the "
                      "original document and save it as PDF again.")
    done, left = [], []
    if kind == ".docx":
        from docx import Document
        doc = Document(str(path))
        if not (doc.core_properties.title or "").strip():
            heading = next((p.text.strip() for p in doc.paragraphs
                            if _heading_level(p.style.name if p.style is not None else "") is not None
                            and p.text.strip()), "")
            doc.core_properties.title = title or heading or path.stem
            done.append(f"title set to \"{doc.core_properties.title}\"")
        if not (doc.core_properties.language or "").strip() and not _docx_default_lang(doc):
            _set_language(doc, language)
            done.append(f"language set to {language}")
        headers = 0
        for t in doc.tables:
            if len(t.rows) > 1 and not _has_header_row(t):
                _mark_header_row(t)
                headers += 1
        if headers:
            done.append(f"{headers} table(s) given a header row (the first row): check that's right")
        if alt_text:
            pics = list(doc.element.body.iter(f"{{{WP}}}docPr"))
            missing = [d for d in pics if _no_alt(d.get("descr") or "")]
            written = await _alt_for_docx(doc, missing[:MAX_ALT_PICTURES])
            if written:
                done.append(f"alt text written for {written} picture(s): check each one")
            if len(missing) > written:
                left.append(f"{len(missing) - written} picture(s) still have no alt text (FreeHand couldn't "
                            "describe them, or there were more than it does at once)")
        target = doc
    else:
        from pptx import Presentation
        from pptx.enum.shapes import MSO_SHAPE_TYPE
        prs = Presentation(str(path))
        if not (prs.core_properties.title or "").strip():
            first = next((_slide_title(s) for s in prs.slides if _slide_title(s)), "")
            prs.core_properties.title = title or first or path.stem
            done.append(f"title set to \"{prs.core_properties.title}\"")
        if not (prs.core_properties.language or "").strip():
            prs.core_properties.language = language
            done.append(f"language set to {language}")
        if alt_text:
            todo = [(n, s) for n, slide in enumerate(prs.slides, 1) for s in slide.shapes
                    if s.shape_type == MSO_SHAPE_TYPE.PICTURE and _no_alt(_shape_alt(s))]
            written = 0
            for n, shape in todo[:MAX_ALT_PICTURES]:
                slide_text = _slide_title(prs.slides[n - 1])
                said = await describe(shape.image.blob, slide_text)
                if said is not None:
                    shape._element._nvXxPr.cNvPr.set("descr", said or "Decorative")
                    written += 1
            if written:
                done.append(f"alt text written for {written} picture(s): check each one")
            if len(todo) > written:
                left.append(f"{len(todo) - written} picture(s) still have no alt text")
        untitled = [n for n, s in enumerate(prs.slides, 1) if not _slide_title(s)]
        if untitled:
            left.append(f"slides {untitled[:15]} have no title: give them one with document_edit")
        target = prs
    if not done:
        return {"nothing_to_fix": True, "file": _rel(path), **({"still_to_do": left} if left else {}),
                "note": "Nothing could be fixed automatically, so no copy was made."}
    out = output_for(path, "accessible", name=output)
    target.save(str(out))
    return {"saved_as": _rel(out), "fixed": done, **({"still_to_do": left} if left else {}),
            "original": f"\"{_rel(path)}\" is unchanged.",
            "next": "Run document_check on the new file to see anything left."}


async def _alt_for_docx(doc, docprs) -> int:
    written = 0
    for docpr in docprs:
        inline = docpr.getparent()
        blip = next(inline.iter(f"{{{A}}}blip"), None) if inline is not None else None
        rid = blip.get(f"{{{R}}}embed") if blip is not None else None
        if not rid or rid not in doc.part.rels:
            continue
        # The paragraph around it, for context.
        node, context = inline, ""
        while node is not None and node.tag != _w("p"):
            node = node.getparent()
        if node is not None:
            prev = node.getprevious()
            context = "".join(t.text or "" for t in prev.iter(_w("t"))) if prev is not None else ""
        said = await describe(doc.part.rels[rid].target_part.blob, context)
        if said is None:
            continue
        docpr.set("descr", said or "Decorative")
        written += 1
    return written
