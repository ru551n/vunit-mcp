"""Tests for the in-process VUnit project model (no simulator needed).

InternalProject.load() builds VUnit in-process from a --export-json file.
These tests exercise its construction against the pinned VUnit fork.
"""

import sys
from pathlib import Path

from vunit_mcp.config import Config
from vunit_mcp.project_model import InternalProject


def _make_config(tmp_path: Path) -> Config:
    project = tmp_path / "proj"
    (project / "rtl").mkdir(parents=True)
    (project / "tb").mkdir()
    (project / "run.py").write_text("print('run')\n", encoding="utf-8")
    (project / "rtl" / "pkg.vhd").write_text("package pkg is end pkg;\n", encoding="utf-8")
    (project / "tb" / "t_a.vhd").write_text("entity t_a is end t_a;\n", encoding="utf-8")
    return Config(
        project_dir=project,
        run_script=project / "run.py",
        python=sys.executable,
        simulator=None,
        output_dir=project / "vunit_out",
        timeout=60.0,
        extra_args=[],
        fingerprint_exclude=[],
    )


def _hostile_export() -> dict:
    """Minimal export with relative file names (the common case)."""
    return {
        "files": [
            {
                "file_name": "rtl/pkg.vhd",
                "file_type": "vhdl",
                "library_name": "rtl",
                "attributes": [],
            },
            {
                "file_name": "tb/t_a.vhd",
                "file_type": "vhdl",
                "library_name": "tb",
                "attributes": [],
            },
        ],
        "tests": [
            {
                "name": "tb.t_a.test1",
                "location": {"file_name": "tb/t_a.vhd", "line_number": 3},
                "attributes": [],
            },
        ],
    }


def test_load_resolves_export_files_against_project_dir(tmp_path, monkeypatch):
    """Export file names are relative to the project dir, not the server's
    cwd (wherever the MCP host launched it)."""
    cfg = _make_config(tmp_path)
    foreign = tmp_path / "elsewhere"
    foreign.mkdir()
    monkeypatch.chdir(foreign)

    project, reused = InternalProject.load(cfg, _hostile_export())
    assert reused is False
    assert "tb.t_a.test1" in project.test_names
    subset = project.implementation_subset(project.resolve_test("tb.t_a.test1")[0])
    assert ("tb", str((cfg.project_dir / "tb" / "t_a.vhd").resolve())) in subset