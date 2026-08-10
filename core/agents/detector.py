"""Detect AI agents installed on the user's system.

Scans standard installation paths for known agents:
- Hermes Agent (NousResearch)
- OpenClaw
"""

import os
import platform
from pathlib import Path
from typing import Dict, List, Optional

from core.agents.models import DetectedAgent


def _windows_hermes_paths() -> List[Path]:
    local_app = os.environ.get("LOCALAPPDATA", "")
    return [Path(local_app) / "hermes"] if local_app else []


def _windows_openclaw_paths() -> List[Path]:
    user_profile = os.environ.get("USERPROFILE", "")
    paths = []
    if user_profile:
        paths.append(Path(user_profile) / ".openclaw")
    app_data = os.environ.get("APPDATA", "")
    if app_data:
        paths.append(Path(app_data) / "openclaw")
    return paths


def _unix_hermes_paths() -> List[Path]:
    paths = []
    home = Path.home()
    paths.append(home / ".hermes")
    paths.append(home / ".local/share/hermes")
    paths.append(Path("/usr/local/share/hermes"))
    return paths


def _unix_openclaw_paths() -> List[Path]:
    home = Path.home()
    paths = []
    paths.append(home / ".openclaw")
    paths.append(home / ".config/openclaw")
    return paths


def _is_hermes_installed(path: Path) -> bool:
    return (path / "SOUL.md").exists() and (path / "config.yaml").exists()


def _is_openclaw_installed(path: Path) -> bool:
    workspace = path / "workspace"
    return (workspace / "SOUL.md").exists() and (workspace / "MEMORY.md").exists()


def _list_hermes_files(path: Path) -> List[Dict[str, str]]:
    files = []
    candidates = [
        ("SOUL.md", "soul"),
        ("AGENTS.md", "agents"),
        ("config.yaml", "config"),
        (".first-setup.json", "config"),
        ("auth.json", "secret"),  # marked secret, never imported by default
    ]
    for fname, ftype in candidates:
        f = path / fname
        if f.exists():
            files.append({
                "path": str(f),
                "relative": fname,
                "type": ftype,
                "size": f.stat().st_size,
            })
    mem_dir = path / "memories"
    if mem_dir.exists():
        for mem_file in ["MEMORY.md", "USER.md"]:
            f = mem_dir / mem_file
            if f.exists():
                files.append({
                    "path": str(f),
                    "relative": f"memories/{mem_file}",
                    "type": "memory" if mem_file == "MEMORY.md" else "user",
                    "size": f.stat().st_size,
                })
    skills_dir = path / "skills"
    if skills_dir.exists():
        for skill_dir in skills_dir.iterdir():
            if skill_dir.is_dir() and (skill_dir / "SKILL.md").exists():
                files.append({
                    "path": str(skill_dir / "SKILL.md"),
                    "relative": f"skills/{skill_dir.name}",
                    "type": "skill",
                    "size": (skill_dir / "SKILL.md").stat().st_size,
                })
    state_db = path / "state.db"
    if state_db.exists():
        files.append({
            "path": str(state_db),
            "relative": "state.db",
            "type": "state_db",
            "size": state_db.stat().st_size,
        })
    return files


def _list_openclaw_files(path: Path) -> List[Dict[str, str]]:
    files = []
    workspace = path / "workspace"
    if not workspace.exists():
        return files
    candidates = [
        ("SOUL.md", "soul"),
        ("IDENTITY.md", "identity"),
        ("MEMORY.md", "memory"),
        ("USER.md", "user"),
        ("AGENTS.md", "agents"),
        ("BOOTSTRAP.md", "bootstrap"),
        ("HEARTBEAT.md", "heartbeat"),
        ("TOOLS.md", "tools"),
    ]
    for fname, ftype in candidates:
        f = workspace / fname
        if f.exists():
            files.append({
                "path": str(f),
                "relative": fname,
                "type": ftype,
                "size": f.stat().st_size,
            })
    mem_dir = workspace / "memory"
    if mem_dir.exists():
        for entry in mem_dir.iterdir():
            if entry.is_file() and entry.suffix == ".md":
                files.append({
                    "path": str(entry),
                    "relative": f"memory/{entry.name}",
                    "type": "journal",
                    "size": entry.stat().st_size,
                })
    for secret_name in ["openclaw.json", "credentials", "exec-approvals.json"]:
        if (path / secret_name).exists() or (workspace / secret_name).exists():
            files.append({
                "path": str(path / secret_name),
                "relative": secret_name,
                "type": "secret",
                "size": (path / secret_name).stat().st_size if (path / secret_name).exists() else 0,
            })
    return files


def _read_version(path: Path, version_files: List[str]) -> Optional[str]:
    for vf in version_files:
        f = path / vf
        if f.exists():
            try:
                import json
                data = json.loads(f.read_text())
                return data.get("version") or data.get("firstSetupAt")
            except Exception:
                continue
    return None


def detect_agents() -> List[DetectedAgent]:
    """Scan the system for installed AI agents."""
    is_windows = platform.system() == "Windows"
    agents = []

    hermes_paths = _windows_hermes_paths() if is_windows else _unix_hermes_paths()
    for path in hermes_paths:
        if _is_hermes_installed(path):
            files = _list_hermes_files(path)
            version = _read_version(path, [".first-setup.json"])
            state_db = path / "state.db" if (path / "state.db").exists() else None
            agents.append(DetectedAgent(
                name="hermes",
                display_name="Hermes Agent",
                path=path,
                files=files,
                version=version,
                state_db_path=state_db,
            ))

    openclaw_paths = _windows_openclaw_paths() if is_windows else _unix_openclaw_paths()
    for path in openclaw_paths:
        if _is_openclaw_installed(path):
            files = _list_openclaw_files(path)
            agents.append(DetectedAgent(
                name="openclaw",
                display_name="OpenClaw",
                path=path,
                files=files,
            ))

    return agents


def detect_agent(name: str) -> Optional[DetectedAgent]:
    """Detect a specific agent by name."""
    for agent in detect_agents():
        if agent.name == name:
            return agent
    return None


def summarize_agents() -> List[Dict]:
    """Return a JSON-serializable summary of detected agents."""
    return [
        {
            "name": a.name,
            "display_name": a.display_name,
            "path": str(a.path),
            "version": a.version,
            "file_count": a.file_count(),
            "total_size": a.total_size(),
            "has_state_db": a.state_db_path is not None,
        }
        for a in detect_agents()
    ]