"""The person's own folders: Hermes, OpenClaw and their notes (Nov 2026).

FreeHand runs in Docker, which keeps it to its own files. That's safer,
but it meant FreeHand couldn't see Hermes, OpenClaw or a notes folder at
all, so "check Hermes for anything new" couldn't work (and FreeHand didn't
say why). compose.yml now shows it just these folders, read-only, at
fixed places (set in .env by scripts/setup_env.py or the installer):

    FREEHAND_HERMES_DIR   -> /host/hermes
    FREEHAND_OPENCLAW_DIR -> /host/openclaw
    FREEHAND_NOTES_DIR    -> /host/notes   (Obsidian vaults, wikis, notes)

Outside Docker, the usual places are used (core/agents/detector.py), and
a notes folder can be named in settings ("notes_dir").

"Bring in what's new" (bring_in_new) copies only what changed since last
time into the vault, where FreeHand's search finds it. Notes come in as
they are (Markdown and plain text), keeping their folders. Nothing in the
person's folders is ever changed.
"""
from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path
from typing import Optional

IN_DOCKER = {"hermes": Path("/host/hermes"), "openclaw": Path("/host/openclaw"), "notes": Path("/host/notes")}
SOURCES = {"hermes": "Hermes", "openclaw": "OpenClaw", "notes": "your notes"}
TEXT_SUFFIXES = {".md", ".markdown", ".txt"}
READABLE_SUFFIXES = TEXT_SUFFIXES | {".csv"}
SKIP_DIRS = {".obsidian", ".git", ".trash", ".stfolder", "node_modules", ".venv", "__pycache__"}
MAX_NOTE_BYTES = 2 * 1024 * 1024
MAX_NOTES = 5000
MAX_READ_CHARS = 60_000
MAX_LISTED = 200


def _has_files(path: Path) -> bool:
    try:
        return path.is_dir() and any(path.iterdir())
    except OSError:
        return False


def shared(source: str) -> Optional[Path]:
    """The folder FreeHand was shown for this source, if any."""
    path = IN_DOCKER.get(source)
    return path if path is not None and _has_files(path) else None


def notes_dir() -> Optional[Path]:
    """The person's notes folder: shown to Docker, or named in settings."""
    found = shared("notes")
    if found:
        return found
    try:
        from core import agent_config
        named = str(agent_config._load_settings().get("notes_dir") or os.environ.get("FREEHAND_NOTES_PATH") or "")
    except Exception:
        named = ""
    path = Path(named).expanduser() if named.strip() else None
    return path if path is not None and _has_files(path) else None


def _same(a: Path, b: Path) -> bool:
    try:
        if a.stat().st_size != b.stat().st_size:
            return False
        return hashlib.sha256(a.read_bytes()).digest() == hashlib.sha256(b.read_bytes()).digest()
    except OSError:
        return False


# ── Notes ────────────────────────────────────────────────────────────

def _note_files(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith("."))
        for name in sorted(filenames):
            path = Path(dirpath) / name
            if path.suffix.lower() in TEXT_SUFFIXES and not name.startswith("."):
                yield path


def import_notes(vault_dir: Path):
    """Copy new and changed notes into vault/imports/notes, keeping their
    folders. Returns an ImportResult (core/agents/models.py)."""
    from core.agents.models import ImportResult
    root = notes_dir()
    result = ImportResult(agent="notes", vault_root=Path(vault_dir) / "imports" / "notes")
    if root is None:
        result.add_error("no notes folder")
        return result
    result.vault_root.mkdir(parents=True, exist_ok=True)
    for count, source in enumerate(_note_files(root)):
        if count >= MAX_NOTES:
            result.add_skipped(f"more than {MAX_NOTES} notes: the rest were left")
            break
        rel = source.relative_to(root)
        try:
            size = source.stat().st_size
            if size > MAX_NOTE_BYTES:
                result.add_skipped(f"too big: {rel}")
                continue
            dest = result.vault_root / rel
            if dest.exists() and _same(source, dest):
                result.add_skipped(f"unchanged: {rel}")
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, dest)
            result.add_file(str(source), str(dest), size, "note")
        except OSError as e:
            result.add_error(f"Couldn't bring in {rel}: {e.strerror or e}")
    return result


# ── Bringing in what's new ───────────────────────────────────────────

def available() -> list[str]:
    """The sources FreeHand can reach on this computer."""
    from core.agents import detect_agent
    found = [s for s in ("hermes", "openclaw") if detect_agent(s)]
    if notes_dir():
        found.append("notes")
    return found


def bring_in_new(source: str = "all") -> dict:
    """Bring in what's new or changed from Hermes, OpenClaw and the notes
    folder. Returns {"done": [sentences], "brought_in": n, "unreachable": [...]}."""
    from core import agent_config
    from core.agents import detect_agent, import_hermes, import_openclaw, record_import
    from core.agents.models import ImportOptions
    vault_dir = Path(agent_config.VAULT_DIR)
    wanted = ["hermes", "openclaw", "notes"] if source in ("", "all", None) else [source]
    done, unreachable, total = [], [], 0
    for name in wanted:
        if name not in SOURCES:
            raise ValueError("Choose hermes, openclaw, notes or all.")
        if name == "notes":
            result = import_notes(vault_dir) if notes_dir() else None
        else:
            agent = detect_agent(name)
            importer = import_hermes if name == "hermes" else import_openclaw
            result = importer(agent, ImportOptions(agent=name, include_state=True, include_skills=True),
                              vault_dir) if agent else None
        if result is None:
            unreachable.append(name)
            continue
        if result.files_imported:
            record_import(result)
        n = len([f for f in result.files_imported if f.get("type") != "state_extracted"])
        total += n
        what = SOURCES[name]
        parts = [f"{n} new or changed file{'s' if n != 1 else ''}" if n else "nothing new"]
        if getattr(result, "state_extracted", False):
            parts.append("its database read again")
        done.append(f"From {what}: " + ", ".join(parts) + ".")
        if result.errors:
            done.append(f"Some of {what} couldn't be read ({len(result.errors)} problem{'s' if len(result.errors) != 1 else ''}).")
    if total:
        try:
            from core import memory
            memory.sync_vault_to_sqlite()
        except Exception:
            pass
    return {"done": done, "brought_in": total, "unreachable": unreachable, "help": unreachable_help(unreachable)}


def in_docker() -> bool:
    return Path("/.dockerenv").exists()


def unreachable_help(names: list) -> str:
    if not names:
        return ""
    labels = [SOURCES[n] for n in names]
    which = labels[0] if len(labels) == 1 else ", ".join(labels[:-1]) + " and " + labels[-1]
    if in_docker():
        return (f"FreeHand can't see {which} on this computer. FreeHand runs in Docker, which only shows it the "
                "folders it's given. Set FREEHAND_HERMES_DIR, FREEHAND_OPENCLAW_DIR or FREEHAND_NOTES_DIR in "
                "FreeHand's .env file to the folder, then restart FreeHand. It reads them and never changes them.")
    return f"FreeHand couldn't find {which} on this computer."


# ── Reading text files and folders ───────────────────────────────────

def read_text_file(path: str) -> dict:
    """A Markdown, text or CSV file, from the folders FreeHand may read."""
    from core.tools.office import check_read_allowed
    denied = check_read_allowed(path)
    if denied:
        return {"error": denied}
    p = Path(path).expanduser()
    if p.suffix.lower() not in READABLE_SUFFIXES:
        return {"error": "read_text_file reads .md, .markdown, .txt and .csv files. For Word, Excel or "
                         "PowerPoint use read_docx, read_xlsx or read_pptx."}
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return {"error": f"There's no file at {path}. Use list_folder to see what's there."}
    except OSError as e:
        return {"error": f"Couldn't read {path}: {e.strerror or e}"}
    out = {"path": str(p), "text": text[:MAX_READ_CHARS]}
    if len(text) > MAX_READ_CHARS:
        out["cut_short"] = f"Only the first {MAX_READ_CHARS} of {len(text)} characters are shown."
    return out


def list_folder(path: str = "") -> dict:
    """What's in a folder FreeHand may read: its folders, and the files it
    can open. No path: the folders FreeHand may read."""
    from core.tools.office import allowed_read_roots, check_read_allowed
    if not str(path or "").strip():
        return {"folders_you_may_read": [str(r) for r in allowed_read_roots()]}
    denied = check_read_allowed(path)
    if denied:
        return {"error": denied}
    p = Path(path).expanduser()
    if not p.is_dir():
        return {"error": f"{path} isn't a folder."}
    folders, files = [], []
    try:
        for child in sorted(p.iterdir(), key=lambda c: c.name.lower()):
            if child.name.startswith(".") or child.name in SKIP_DIRS:
                continue
            if child.is_dir():
                folders.append(child.name)
            elif child.suffix.lower() in READABLE_SUFFIXES | {".docx", ".xlsx", ".pptx"}:
                files.append(child.name)
    except OSError as e:
        return {"error": f"Couldn't open {path}: {e.strerror or e}"}
    out = {"path": str(p), "folders": folders[:MAX_LISTED], "files": files[:MAX_LISTED]}
    if len(folders) > MAX_LISTED or len(files) > MAX_LISTED:
        out["cut_short"] = f"Only the first {MAX_LISTED} of each are listed."
    return out
