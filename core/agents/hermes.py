"""Hermes Agent importer.

Imports safe .md/.yaml files from Hermes Agent installation.
Skips secrets (auth.json).
"""

import shutil
from pathlib import Path
from typing import List

from core.agents.models import DetectedAgent, ImportOptions, ImportResult
from core.agents.state_extractor import extract_hermes_state, get_state_db_tables

SAFE_TYPES = {"soul", "memory", "user", "agents", "config", "skill"}
SKIP_TYPES = {"secret", "state_db"}

VAULT_SUBDIR = "hermes"
SKILLS_VAULT_SUBDIR = "hermes-imports"


def _safe_filename(name: str) -> str:
    return name.replace("/", "_").replace("\\", "_")


def preview_hermes(agent: DetectedAgent, options: ImportOptions) -> dict:
    """Show what would be imported without writing anything."""
    preview = {
        "agent": "hermes",
        "vault_subdir": VAULT_SUBDIR,
        "files": [],
        "skills": [],
        "state_extraction": None,
    }
    for f in agent.files:
        if f["type"] in SKIP_TYPES:
            continue
        if f["type"] not in SAFE_TYPES:
            continue
        if f["type"] == "skill" and not options.include_skills:
            continue
        preview["files"].append({
            "source": f["relative"],
            "type": f["type"],
            "size": f["size"],
        })
        if f["type"] == "skill":
            preview["skills"].append(f["relative"])

    if options.include_state and agent.state_db_path:
        tables = get_state_db_tables(agent.state_db_path)
        keep_tables = [t for t in tables if t["classification"] == "keep"]
        preview["state_extraction"] = {
            "source": "state.db",
            "tables_found": len(tables),
            "tables_to_extract": len(keep_tables),
            "sample_tables": [t["name"] for t in keep_tables[:10]],
        }
    return preview


def import_hermes(agent: DetectedAgent, options: ImportOptions, vault_dir: Path) -> ImportResult:
    """Import safe files from Hermes Agent into FreeHand vault."""
    result = ImportResult(agent="hermes", vault_root=vault_dir / "imports" / VAULT_SUBDIR)
    result.vault_root.mkdir(parents=True, exist_ok=True)

    for f in agent.files:
        if f["type"] in SKIP_TYPES:
            result.add_skipped(f"secret file: {f['relative']}")
            continue
        if f["type"] not in SAFE_TYPES:
            result.add_skipped(f"unsafe type {f['type']}: {f['relative']}")
            continue

        source_path = Path(f["path"])
        if not source_path.exists():
            result.add_skipped(f"file not found: {f['relative']}")
            continue

        try:
            if f["type"] == "skill":
                if not options.include_skills:
                    continue
                skill_name = source_path.parent.name
                dest_skill_dir = vault_dir / "skills" / SKILLS_VAULT_SUBDIR / skill_name
                dest_skill_dir.mkdir(parents=True, exist_ok=True)
                if source_path.is_file():
                    shutil.copy2(source_path, dest_skill_dir / "SKILL.md")
                    result.add_file(f["path"], str(dest_skill_dir / "SKILL.md"), f["size"], "skill")
                    result.add_skill(skill_name)
                continue

            vault_name = _safe_filename(f["relative"])
            dest = result.vault_root / vault_name
            if dest.exists() and not options.overwrite:
                result.add_skipped(f"already exists: {vault_name}")
                continue
            shutil.copy2(source_path, dest)
            result.add_file(f["path"], str(dest), f["size"], f["type"])
        except Exception as e:
            result.add_error(f"Failed to import {f['relative']}: {e}")

    if options.include_state and agent.state_db_path and agent.state_db_path.exists():
        state_dest = result.vault_root / "state_memory.md"
        try:
            extracted, errors = extract_hermes_state(agent.state_db_path, state_dest)
            if extracted > 0:
                result.state_extracted = True
                result.add_file(
                    str(agent.state_db_path),
                    str(state_dest),
                    state_dest.stat().st_size,
                    "state_extracted",
                )
            for err in errors:
                result.add_error(err)
        except Exception as e:
            result.add_error(f"State extraction failed: {e}")

    return result