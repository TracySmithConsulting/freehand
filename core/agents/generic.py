"""Generic .md file importer for ad-hoc agent memory directories."""

import shutil
from pathlib import Path
from typing import List

from core.agents.models import DetectedAgent, ImportOptions, ImportResult


def import_generic(agent_name: str, source_dir: Path, vault_dir: Path, options: ImportOptions) -> ImportResult:
    """Import all .md files from a given directory."""
    result = ImportResult(agent=agent_name, vault_root=vault_dir / "imports" / agent_name)
    result.vault_root.mkdir(parents=True, exist_ok=True)

    if not source_dir.exists() or not source_dir.is_dir():
        result.add_error(f"Source directory not found: {source_dir}")
        return result

    md_files = sorted(source_dir.rglob("*.md"))
    for src in md_files:
        rel = src.relative_to(source_dir)
        dest_name = str(rel).replace("/", "_").replace("\\", "_")
        dest = result.vault_root / dest_name
        try:
            if dest.exists() and not options.overwrite:
                result.add_skipped(f"already exists: {dest_name}")
                continue
            shutil.copy2(src, dest)
            result.add_file(str(src), str(dest), src.stat().st_size, "generic")
        except Exception as e:
            result.add_error(f"Failed to import {dest_name}: {e}")

    return result