"""Tests for cross-agent detection and import."""

import sys
sys.path.insert(0, r"C:\Users\trace\Documents\Default Project")

import pytest
from pathlib import Path
from core.agents import detect_agents, detect_agent, summarize_agents
from core.agents.models import ImportOptions
from core.agents.hermes import preview_hermes, import_hermes
from core.agents.openclaw import preview_openclaw, import_openclaw
from core.agents.merger import record_import, get_import_status, list_imports, remove_import
from core.database import init_db


class TestDetection:
    def test_detect_hermes(self):
        agents = detect_agents()
        hermes = [a for a in agents if a.name == "hermes"]
        # Hermes may or may not be installed
        if hermes:
            assert hermes[0].display_name == "Hermes Agent"
            assert hermes[0].file_count() > 0

    def test_detect_openclaw(self):
        agents = detect_agents()
        oc = [a for a in agents if a.name == "openclaw"]
        if oc:
            assert oc[0].display_name == "OpenClaw"
            assert oc[0].file_count() > 0

    def test_summarize(self):
        summary = summarize_agents()
        assert isinstance(summary, list)
        for a in summary:
            assert "name" in a
            assert "display_name" in a
            assert "path" in a
            assert "file_count" in a


class TestHermesImport:
    def test_preview(self):
        agents = detect_agents()
        hermes = next((a for a in agents if a.name == "hermes"), None)
        if not hermes:
            pytest.skip("Hermes not installed")
        opts = ImportOptions(agent="hermes", include_state=True)
        preview = preview_hermes(hermes, opts)
        assert "files" in preview
        assert "skills" in preview
        assert len(preview["files"]) > 0

    def test_import(self):
        agents = detect_agents()
        hermes = next((a for a in agents if a.name == "hermes"), None)
        if not hermes:
            pytest.skip("Hermes not installed")
        init_db()
        vault = Path(r"C:\Users\trace\Documents\Default Project\vault")
        import shutil
        shutil.rmtree(vault / "imports" / "hermes", ignore_errors=True)
        result = import_hermes(hermes, ImportOptions(agent="hermes", include_state=True, overwrite=True), vault)
        assert len(result.files_imported) > 0
        record_import(result)


class TestOpenClawImport:
    def test_preview(self):
        agents = detect_agents()
        oc = next((a for a in agents if a.name == "openclaw"), None)
        if not oc:
            pytest.skip("OpenClaw not installed")
        opts = ImportOptions(agent="openclaw")
        preview = preview_openclaw(oc, opts)
        assert "files" in preview
        assert len(preview["files"]) > 0

    def test_import(self):
        agents = detect_agents()
        oc = next((a for a in agents if a.name == "openclaw"), None)
        if not oc:
            pytest.skip("OpenClaw not installed")
        init_db()
        vault = Path(r"C:\Users\trace\Documents\Default Project\vault")
        import shutil
        shutil.rmtree(vault / "imports" / "openclaw", ignore_errors=True)
        result = import_openclaw(oc, ImportOptions(agent="openclaw", overwrite=True), vault)
        assert len(result.files_imported) > 0
        record_import(result)


class TestMerger:
    def test_list_imports(self):
        imports = list_imports()
        assert isinstance(imports, list)

    def test_get_status(self):
        status = get_import_status()
        assert "total_imports" in status
        assert "total_files" in status
        assert "agents" in status
