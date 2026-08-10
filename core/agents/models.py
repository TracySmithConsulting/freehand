"""Data models for cross-agent detection and import."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional


@dataclass
class DetectedAgent:
    name: str
    display_name: str
    path: Path
    files: List[Dict[str, str]] = field(default_factory=list)
    version: Optional[str] = None
    state_db_path: Optional[Path] = None

    def file_count(self) -> int:
        return len(self.files)

    def total_size(self) -> int:
        return sum(int(f.get("size", 0)) for f in self.files)


@dataclass
class ImportOptions:
    agent: str
    include_state: bool = False
    include_skills: bool = True
    dry_run: bool = False
    overwrite: bool = False


@dataclass
class ImportResult:
    agent: str
    vault_root: Path
    files_imported: List[Dict[str, str]] = field(default_factory=list)
    skills_imported: List[str] = field(default_factory=list)
    state_extracted: bool = False
    errors: List[str] = field(default_factory=list)
    skipped: List[str] = field(default_factory=list)

    def add_file(self, source_path: str, vault_path: str, size: int, file_type: str):
        self.files_imported.append({
            "source": source_path,
            "vault": vault_path,
            "size": size,
            "type": file_type,
        })

    def add_skill(self, skill_name: str):
        self.skills_imported.append(skill_name)

    def add_error(self, error: str):
        self.errors.append(error)

    def add_skipped(self, reason: str):
        self.skipped.append(reason)

    def summary(self) -> Dict:
        return {
            "agent": self.agent,
            "vault_root": str(self.vault_root),
            "files_imported": len(self.files_imported),
            "skills_imported": len(self.skills_imported),
            "state_extracted": self.state_extracted,
            "errors": len(self.errors),
            "skipped": len(self.skipped),
        }