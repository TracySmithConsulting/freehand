"""Footnotes, a contents page, and keeping lists and footnotes when Word
documents are combined (Nov 2026: assembling the DBSA manual).

python-docx has no footnotes, so they're made here in Word's own XML:
- a footnotes part, with the two separator notes Word expects;
- "Footnote Text" and "Footnote Reference" styles;
- a numbered reference in the text.

A screen reader announces each one, and Word numbers them through the
whole document.

The contents page is a real Word contents field. It's filled in with the
headings now, so it's useful straight away, and Word adds page numbers
when the document is opened (Word asks whether to update fields: Yes).

Combining copies each document's paragraphs into the first. Their list
numbering and footnotes point at the source document's definitions, so
they're copied across too, with new numbers. Without this, bullets could
turn into numbers or vanish, and footnotes would be lost.
"""
from __future__ import annotations

import copy
import re
from typing import Dict, List

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
FOOTNOTES_RT = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/footnotes"
FOOTNOTES_CT = "application/vnd.openxmlformats-officedocument.wordprocessingml.footnotes+xml"


def _w(tag: str) -> str:
    return f"{{{W}}}{tag}"


def _register_footnotes_part() -> None:
    """Load footnotes.xml as XML (not raw bytes), so it can be changed."""
    from docx.opc.part import PartFactory, XmlPart
    PartFactory.part_type_for.setdefault(FOOTNOTES_CT, XmlPart)


_register_footnotes_part()


# ── Text that's safe in Word ───────────────────────────────────────────

CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")


def as_text(value) -> str:
    """Words from what a model sent: a string, or {"text": ...}, or a list
    of either (it has sent all three). Characters Word can't store are
    taken out (they stopped a Chapter 4 edit with "All strings must be XML
    compatible")."""
    if isinstance(value, dict):
        value = value.get("text", "")
    if isinstance(value, (list, tuple)):
        value = "".join(as_text(v) for v in value)
    return CONTROL.sub("", "" if value is None else str(value))


# ── Styles ─────────────────────────────────────────────────────────────

def _ensure_style(doc, style_id: str, name: str, kind: str, xml_inner: str) -> None:
    from docx.oxml import parse_xml
    styles = doc.styles.element
    for s in styles.findall(_w("style")):
        if s.get(_w("styleId")) == style_id:
            return
    styles.append(parse_xml(
        f'<w:style xmlns:w="{W}" w:type="{kind}" w:styleId="{style_id}">'
        f'<w:name w:val="{name}"/>{xml_inner}</w:style>'))


def ensure_footnote_styles(doc) -> None:
    _ensure_style(doc, "FootnoteText", "footnote text", "paragraph",
                  '<w:basedOn w:val="Normal"/><w:uiPriority w:val="99"/>'
                  '<w:pPr><w:spacing w:after="0" w:line="240" w:lineRule="auto"/></w:pPr>'
                  '<w:rPr><w:sz w:val="20"/><w:szCs w:val="20"/></w:rPr>')
    _ensure_style(doc, "FootnoteReference", "footnote reference", "character",
                  '<w:uiPriority w:val="99"/><w:rPr><w:vertAlign w:val="superscript"/></w:rPr>')


def ensure_contents_styles(doc) -> None:
    _ensure_style(doc, "TOCHeading", "TOC Heading", "paragraph",
                  '<w:basedOn w:val="Heading1"/><w:next w:val="Normal"/><w:uiPriority w:val="39"/>'
                  '<w:pPr><w:outlineLvl w:val="9"/></w:pPr>')
    for level in (1, 2, 3):
        _ensure_style(doc, f"TOC{level}", f"toc {level}", "paragraph",
                      f'<w:basedOn w:val="Normal"/><w:next w:val="Normal"/><w:uiPriority w:val="39"/>'
                      f'<w:pPr><w:spacing w:after="100"/><w:ind w:left="{(level - 1) * 240}"/></w:pPr>')


# ── Footnotes ──────────────────────────────────────────────────────────

def footnotes(doc, create: bool = False):
    """The document's <w:footnotes> element, made if asked and missing."""
    for rel in doc.part.rels.values():
        if rel.reltype == FOOTNOTES_RT and not rel.is_external:
            return rel.target_part.element
    if not create:
        return None
    from docx.opc.packuri import PackURI
    from docx.opc.part import XmlPart
    from docx.oxml import parse_xml
    xml = (f'<w:footnotes xmlns:w="{W}">'
           f'<w:footnote w:type="separator" w:id="-1"><w:p><w:pPr><w:spacing w:after="0" w:line="240" '
           f'w:lineRule="auto"/></w:pPr><w:r><w:separator/></w:r></w:p></w:footnote>'
           f'<w:footnote w:type="continuationSeparator" w:id="0"><w:p><w:pPr><w:spacing w:after="0" '
           f'w:line="240" w:lineRule="auto"/></w:pPr><w:r><w:continuationSeparator/></w:r></w:p></w:footnote>'
           f'</w:footnotes>')
    part = XmlPart(PackURI("/word/footnotes.xml"), FOOTNOTES_CT, parse_xml(xml), doc.part.package)
    doc.part.relate_to(part, FOOTNOTES_RT)
    return part.element


def _next_id(notes) -> int:
    ids = [int(n.get(_w("id"))) for n in notes.findall(_w("footnote"))]
    return max([0, *ids]) + 1


def _reference_run(note_id: int):
    from docx.oxml import parse_xml
    return parse_xml(f'<w:r xmlns:w="{W}"><w:rPr><w:rStyle w:val="FootnoteReference"/></w:rPr>'
                     f'<w:footnoteReference w:id="{note_id}"/></w:r>')


def _new_note(notes, text: str) -> int:
    from docx.oxml import parse_xml
    from xml.sax.saxutils import escape
    note_id = _next_id(notes)
    notes.append(parse_xml(
        f'<w:footnote xmlns:w="{W}" w:id="{note_id}"><w:p><w:pPr><w:pStyle w:val="FootnoteText"/></w:pPr>'
        f'<w:r><w:rPr><w:rStyle w:val="FootnoteReference"/></w:rPr><w:footnoteRef/></w:r>'
        f'<w:r><w:t xml:space="preserve"> {escape(text)}</w:t></w:r></w:p></w:footnote>'))
    return note_id


def add_footnote(doc, paragraph, text: str, after: str = "") -> str:
    """A footnote in a paragraph: after the words in ``after`` (their first
    appearance), or at the end of the paragraph. Returns what was done, or
    raises ValueError if those words aren't in the paragraph."""
    text = as_text(text).strip()
    if not text:
        raise ValueError("A footnote needs its text.")
    after = as_text(after).strip()
    runs = [r for r in paragraph.runs if r.text]
    whole = "".join(r.text for r in runs)
    if after:
        at = whole.find(after)
        if at < 0:
            at = whole.lower().find(after.lower())
        if at < 0:
            raise ValueError(f"\"{after[:50]}\" isn't in that paragraph")
        end = at + len(after)
        while end < len(whole) and whole[end].isalnum():
            end += 1  # "after estimate" in "estimates": the mark goes after the whole word
    else:
        end = len(whole)
    ensure_footnote_styles(doc)
    note_id = _new_note(footnotes(doc, create=True), text)
    ref = _reference_run(note_id)
    if not runs:
        paragraph._p.append(ref)
        return "a footnote added"
    seen = 0
    for run in runs:
        start, stop = seen, seen + len(run.text)
        seen = stop
        if end > stop:
            continue
        if end < stop:
            # Split the run so the footnote mark sits right after the words.
            tail = copy.deepcopy(run._r)
            run._r.addnext(tail)
            from docx.text.run import Run
            Run(tail, run._parent).text = run.text[end - start:]
            run.text = run.text[:end - start]
        run._r.addnext(ref)
        break
    return "a footnote added" + (f" after \"{after[:40]}\"" if after else "")


def list_footnotes(doc) -> List[dict]:
    """Each footnote: its number in reading order, its text, and the
    paragraph it's in."""
    notes = footnotes(doc)
    if notes is None:
        return []
    by_id = {n.get(_w("id")): n for n in notes.findall(_w("footnote"))}
    index = {p._p: i for i, p in enumerate(doc.paragraphs)}
    out = []
    for ref in doc.element.body.iter(_w("footnoteReference")):
        note = by_id.get(ref.get(_w("id")))
        if note is None:
            continue
        text = "".join(t.text or "" for t in note.iter(_w("t"))).strip()
        el = ref.getparent()
        while el is not None and el.tag != _w("p"):
            el = el.getparent()
        out.append({"footnote": len(out) + 1, "text": text,
                    **({"p": index[el]} if el is not None and el in index else {})})
    return out


# ── A contents page ────────────────────────────────────────────────────

def add_contents(doc, anchor, levels: int = 2, heading: str = "Contents") -> str:
    """A "Contents" heading and a Word contents field after a paragraph,
    listing the headings that follow it, up to ``levels`` deep."""
    from docx.oxml import parse_xml
    from xml.sax.saxutils import escape
    levels = max(1, min(int(levels or 2), 3))
    ensure_contents_styles(doc)
    from core.documents import _heading_level
    paragraphs = list(doc.paragraphs)
    at = next(i for i, p in enumerate(paragraphs) if p._p is anchor._p)
    entries = []
    for p in paragraphs[at + 1:]:
        level = _heading_level(p.style.name if p.style is not None else "")
        if level and level <= levels and p.text.strip():
            entries.append((level, p.text.strip()))
    if not entries:
        raise ValueError("There are no headings after that paragraph to list.")
    title = parse_xml(f'<w:p xmlns:w="{W}"><w:pPr><w:pStyle w:val="TOCHeading"/></w:pPr>'
                      f'<w:r><w:t>{escape(heading)}</w:t></w:r></w:p>')
    field_paragraphs = []
    for i, (level, text) in enumerate(entries):
        begin = ""
        if i == 0:
            begin = (f'<w:r><w:fldChar w:fldCharType="begin" w:dirty="true"/></w:r>'
                     f'<w:r><w:instrText xml:space="preserve"> TOC \\o "1-{levels}" \\h \\z \\u </w:instrText></w:r>'
                     f'<w:r><w:fldChar w:fldCharType="separate"/></w:r>')
        end = '<w:r><w:fldChar w:fldCharType="end"/></w:r>' if i == len(entries) - 1 else ""
        field_paragraphs.append(parse_xml(
            f'<w:p xmlns:w="{W}"><w:pPr><w:pStyle w:val="TOC{level}"/></w:pPr>{begin}'
            f'<w:r><w:t xml:space="preserve">{escape(text)}</w:t></w:r>{end}</w:p>'))
    anchor._p.addnext(title)
    last = title
    for fp in field_paragraphs:
        last.addnext(fp)
        last = fp
    # Word fills in the page numbers when it opens the file.
    settings = doc.settings.element
    if settings.find(_w("updateFields")) is None:
        settings.append(parse_xml(f'<w:updateFields xmlns:w="{W}" w:val="true"/>'))
    return f"a contents page added after paragraph {at} ({len(entries)} headings)"


# ── Combining: lists and footnotes come across ─────────────────────────

def _numbering_or_none(doc):
    try:
        return doc.part.numbering_part.element
    except (KeyError, NotImplementedError):
        return None


def copy_lists(source, target, element, mapping: Dict[str, str]) -> None:
    """Point copied list items at copies of their list definitions in the
    target. ``mapping`` (source numId -> target numId) is kept per source
    document, so one list stays one list."""
    src = _numbering_or_none(source)
    refs = [n for n in element.iter(_w("numId")) if n.getparent().tag == _w("numPr")]
    if src is None or not refs:
        return
    from core.documents import _numbering
    dst = _numbering(target)
    for ref in refs:
        old = ref.get(_w("val"))
        if old in (None, "0"):
            continue
        if old not in mapping:
            num = next((n for n in src.findall(_w("num")) if n.get(_w("numId")) == old), None)
            if num is None:
                continue
            abstract_ref = num.find(_w("abstractNumId"))
            abstract = next((a for a in src.findall(_w("abstractNum"))
                             if abstract_ref is not None and a.get(_w("abstractNumId")) == abstract_ref.get(_w("val"))),
                            None)
            if abstract is None:
                continue
            new_abstract_id = str(max([-1, *[int(a.get(_w("abstractNumId"))) for a in dst.findall(_w("abstractNum"))]]) + 1)
            new_num_id = str(max([0, *[int(n.get(_w("numId"))) for n in dst.findall(_w("num"))]]) + 1)
            a = copy.deepcopy(abstract)
            a.set(_w("abstractNumId"), new_abstract_id)
            for nsid in a.findall(_w("nsid")):
                a.remove(nsid)  # Word joins lists that share an nsid
            first_num = dst.find(_w("num"))
            if first_num is not None:
                first_num.addprevious(a)
            else:
                dst.append(a)
            n = copy.deepcopy(num)
            n.set(_w("numId"), new_num_id)
            n.find(_w("abstractNumId")).set(_w("val"), new_abstract_id)
            dst.append(n)
            mapping[old] = new_num_id
        ref.set(_w("val"), mapping[old])


def copy_footnotes(source, target, element) -> None:
    """Copy the footnotes a copied element refers to, with new numbers."""
    refs = list(element.iter(_w("footnoteReference")))
    src = footnotes(source) if refs else None
    if src is None:
        return
    ensure_footnote_styles(target)
    dst = footnotes(target, create=True)
    by_id = {n.get(_w("id")): n for n in src.findall(_w("footnote"))}
    for ref in refs:
        note = by_id.get(ref.get(_w("id")))
        if note is None:
            continue
        new_id = _next_id(dst)
        n = copy.deepcopy(note)
        n.set(_w("id"), str(new_id))
        dst.append(n)
        ref.set(_w("id"), str(new_id))
