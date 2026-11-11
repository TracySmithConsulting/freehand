"""Office document processing tools.

Wraps python-docx, openpyxl, and python-pptx for reading and writing
.docx, .xlsx, and .pptx files. All writes are checked against the
project workspace boundary — writes outside the project root require
approval via intercept_action().
"""

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import sys
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from core.security import intercept_action, PROJECT_ROOT


def _is_within(path: str, root: Path) -> bool:
    """True if ``path`` resolves to ``root`` or somewhere beneath it.

    Uses a real path-component comparison. The old string-prefix check
    treated a sibling like ``<root>2/secret`` as inside ``<root>``.
    """
    try:
        return Path(path).resolve().is_relative_to(Path(root).resolve())
    except Exception:
        return False


def _is_within_workspace(path: str) -> bool:
    """Check whether a file path is inside the project workspace."""
    return _is_within(path, PROJECT_ROOT)


def allowed_read_roots() -> List[Path]:
    """Folders the agent may read documents from.

    Always the vault; plus any folders the owner lists in
    settings["allowed_read_dirs"]. Read dynamically so tests (and a
    settings change) take effect without a restart.
    """
    from core import agent_config
    roots = [Path(agent_config.VAULT_DIR)]
    try:
        extra = agent_config._load_settings().get("allowed_read_dirs") or []
    except Exception:
        extra = []
    if isinstance(extra, str):
        extra = [extra]
    for d in extra:
        if isinstance(d, str) and d.strip():
            roots.append(Path(d.strip()).expanduser())
    return roots


def check_read_allowed(path: str) -> Optional[str]:
    """Return None if ``path`` may be read by the agent, else an error message.

    Agent- and API-driven document reads are confined to
    allowed_read_roots(). Without this, a prompt-injected instruction in
    an email or web page could make the agent read any file on the machine
    (e.g. .env, SSH keys) and then post it somewhere. The local CLI calls
    read_docx() etc. directly and is not confined.
    """
    if not path or not str(path).strip():
        return "path is required"
    roots = allowed_read_roots()
    if any(_is_within(path, r) for r in roots):
        return None
    listed = ", ".join(str(r) for r in roots)
    return (
        f"Reading {path} is not allowed. The agent can only read files in: "
        f"{listed}. Add a folder to settings 'allowed_read_dirs' to allow more."
    )


def _check_write_permission(path: str) -> dict:
    """Return approval result for a write operation."""
    if _is_within_workspace(path):
        return {"allowed": True, "approval_id": None}
    return intercept_action(
        action_type="write_file",
        description=f"Write to {path}",
        payload={"path": path, "within_workspace": False},
    )


def read_docx(path: str) -> dict:
    """Read a .docx file and return its structure.

    Returns dict with:
        title (str): first heading or filename
        paragraphs (list[str]): all paragraph texts
        tables (list[list[list[str]]]): table data
        metadata (dict): creator, created/modified dates
    """
    from docx import Document

    p = Path(path)
    if not p.exists():
        return {"error": f"File not found: {path}"}

    doc = Document(str(p))
    paragraphs = [para.text for para in doc.paragraphs if para.text.strip()]

    tables = []
    for table in doc.tables:
        row_data = []
        for row in table.rows:
            row_data.append([cell.text.strip() for cell in row.cells])
        tables.append(row_data)

    metadata = {}
    core_properties = doc.core_properties
    if core_properties.title:
        metadata["title"] = core_properties.title
    if core_properties.author:
        metadata["author"] = core_properties.author
    if core_properties.created:
        metadata["created"] = str(core_properties.created)
    if core_properties.modified:
        metadata["modified"] = str(core_properties.modified)

    title = paragraphs[0][:80] if paragraphs else p.stem
    return {
        "path": str(p.resolve()),
        "title": title,
        "paragraph_count": len(paragraphs),
        "table_count": len(tables),
        "paragraphs": paragraphs[:100],
        "tables": tables[:10],
        "metadata": metadata,
    }


def write_docx(path: str, title: str, content: List[str]) -> dict:
    """Create a new .docx file with the given title and paragraph content.

    Args:
        path: Output file path (.docx)
        title: Document title (first heading)
        content: List of paragraph strings

    Returns dict with status and path info.
    """
    result = _check_write_permission(path)
    if not result.get("allowed", False):
        return {
            "error": "Permission denied",
            "approval_id": result.get("approval_id"),
            "message": "Write action requires approval. Use /approve <id> to allow.",
        }

    from docx import Document
    from docx.shared import Pt
    from docx.enum.text import WD_ALIGN_PARAGRAPH

    doc = Document()

    heading = doc.add_heading(title, level=0)
    heading.alignment = WD_ALIGN_PARAGRAPH.CENTER

    for text in content:
        doc.add_paragraph(text)

    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(p))

    return {
        "status": "saved",
        "path": str(p.resolve()),
        "title": title,
        "paragraphs": len(content),
    }


def read_xlsx(path: str) -> dict:
    """Read an .xlsx file and return its structure.

    Returns dict with:
        sheets (dict): sheet_name -> list of rows
        metadata (dict): sheet count, dimensions
    """
    from openpyxl import load_workbook

    p = Path(path)
    if not p.exists():
        return {"error": f"File not found: {path}"}

    wb = load_workbook(str(p), data_only=True)
    sheets = {}
    metadata = {
        "sheet_names": wb.sheetnames,
        "sheet_count": len(wb.sheetnames),
    }

    for sheet_name in wb.sheetnames:
        ws = wb[sheet_name]
        row_data = []
        for row in ws.iter_rows(values_only=True):
            row_data.append([str(cell) if cell is not None else "" for cell in row])
        sheets[sheet_name] = row_data[:200]
        metadata[f"{sheet_name}_rows"] = ws.max_row
        metadata[f"{sheet_name}_cols"] = ws.max_column

    wb.close()
    return {
        "path": str(p.resolve()),
        "sheets": sheets,
        "metadata": metadata,
    }


def write_xlsx(path: str, data: Dict[str, List[List[Any]]]) -> dict:
    """Create an .xlsx file with multiple sheets.

    Args:
        path: Output file path (.xlsx)
        data: Dict mapping sheet_name -> list of row lists

    Returns dict with status and path info.
    """
    result = _check_write_permission(path)
    if not result.get("allowed", False):
        return {
            "error": "Permission denied",
            "approval_id": result.get("approval_id"),
            "message": "Write action requires approval. Use /approve <id> to allow.",
        }

    from openpyxl import Workbook

    wb = Workbook()
    for sheet_name, rows in data.items():
        ws = wb.create_sheet(title=sheet_name[:31])
        for row in rows:
            ws.append(row)

    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    wb.save(str(p))
    wb.close()

    return {
        "status": "saved",
        "path": str(p.resolve()),
        "sheets": list(data.keys()),
    }


def read_pptx(path: str) -> dict:
    """Read a .pptx file and return its structure.

    Returns dict with:
        slides (list[dict]): each slide's title and text content
        metadata (dict): slide count, author, etc.
    """
    from pptx import Presentation

    p = Path(path)
    if not p.exists():
        return {"error": f"File not found: {path}"}

    prs = Presentation(str(p))
    slides = []

    for i, slide in enumerate(prs.slides, 1):
        slide_data = {"index": i, "title": "", "texts": []}
        for shape in slide.shapes:
            if hasattr(shape, "text") and shape.text.strip():
                text = shape.text.strip()
                slide_data["texts"].append(text)
                if not slide_data["title"] and shape.has_text_frame:
                    first_para = shape.text_frame.paragraphs[0].text.strip()
                    if first_para and len(first_para) < 100:
                        slide_data["title"] = first_para
        slides.append(slide_data)

    metadata = {
        "slide_count": len(prs.slides),
        "width": prs.slide_width.inches,
        "height": prs.slide_height.inches,
    }

    return {
        "path": str(p.resolve()),
        "slides": slides,
        "metadata": metadata,
    }


def write_pptx(path: str, slides: List[Dict]) -> dict:
    """Create a .pptx file from slide definitions.

    Args:
        path: Output file path (.pptx)
        slides: List of dicts with 'title' and 'content' (list of strings)

    Returns dict with status and path info.
    """
    result = _check_write_permission(path)
    if not result.get("allowed", False):
        return {
            "error": "Permission denied",
            "approval_id": result.get("approval_id"),
            "message": "Write action requires approval. Use /approve <id> to allow.",
        }

    from pptx import Presentation
    from pptx.util import Inches, Pt
    from pptx.enum.text import PP_ALIGN

    prs = Presentation()
    blank_layout = prs.slide_layouts[6]

    for slide_data in slides:
        slide = prs.slides.add_slide(blank_layout)
        title = slide_data.get("title", "")
        content = slide_data.get("content", [])

        if title:
            title_box = slide.shapes.add_textbox(Inches(0.5), Inches(0.3), Inches(9), Inches(0.8))
            tf = title_box.text_frame
            tf.word_wrap = True
            p = tf.paragraphs[0]
            p.text = title
            p.font.size = Pt(32)
            p.font.bold = True

        if content:
            content_box = slide.shapes.add_textbox(Inches(0.5), Inches(1.2), Inches(9), Inches(5))
            tf = content_box.text_frame
            tf.word_wrap = True
            for i, text in enumerate(content):
                if i > 0:
                    p = tf.add_paragraph()
                else:
                    p = tf.paragraphs[0]
                p.text = text
                p.font.size = Pt(18)
                p.space_after = Pt(8)

    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    prs.save(str(p))

    return {
        "status": "saved",
        "path": str(p.resolve()),
        "slides": len(slides),
    }
