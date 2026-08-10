"""OpenClaw importer.

Imports safe workspace files from OpenClaw installation.
Skips secrets (openclaw.json, credentials/, exec-approvals.json).
"""

import shutil
from pathlib import Path

from core.agents.models import DetectedAgent, ImportOptions, ImportResult

SAFE_TYPES = {
    "soul", "identity", "memory", "user", "agents",
    "bootstrap", "heartbeat", "tools", "journal",
}
SKIP_TYPES = {"secret"}

VAULT_SUBDIR = "openclaw"
JOURNALS_VAULT_SUBDIR = "memory"


def _safe_filename(name: str) -> str:
    return name.replace("/", "_").replace("\\", "_")


def preview_openclaw(agent: DetectedAgent, options: ImportOptions) -> dict:
    """Show what would be imported without writing anything."""
    preview = {
        "agent": "openclaw",
        "vault_subdir": VAULT_SUBDIR,
        "files": [],
    }
    for f in agent.files:
        if f["type"] in SKIP_TYPES:
            continue
        if f["type"] not in SAFE_TYPES:
            continue
        preview["files"].append({
            "source": f["relative"],
            "type": f["type"],
            "size": f["size"],
        })
    return preview


def import_openclaw(agent: DetectedAgent, options: ImportOptions, vault_dir: Path) -> ImportResult:
    """Import safe workspace files from OpenClaw into FreeHand vault."""
    result = ImportResult(agent="openclaw", vault_root=vault_dir / "imports" / VAULT_SUBDIR)
    result.vault_root.mkdir(parents=True, exist_ok=True)

    journals_dir = result.vault_root / JOURNALS_VAULT_SUBDIR
    journals_dir.mkdir(parents=True, exist_ok=True)

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
            if f["type"] == "journal":
                dest_name = _safe_filename(f["relative"].replace("memory/", ""))
                dest = journals_dir / dest_name
                if dest.exists() and not options.overwrite:
                    result.add_skipped(f"already exists: memory/{dest_name}")
                    continue
                shutil.copy2(source_path, dest)
                result.add_file(f["path"], str(dest), f["size"], "journal")
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

    return result