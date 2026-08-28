"""Tests for the in-process VUnit project model (no simulator needed).

InternalProject.load() builds VUnit in-process from the --export-json file.
These tests exercise its construction against the pinned VUnit fork —
including the scratch-dir wipe that stops a hostile project from planting
a pickled database there (code execution in the server process).
"""

import pickle
import struct
import sys
from pathlib import Path

import pytest

from vunit_mcp.config import Config
from vunit_mcp.project_model import InternalProject, _export_key, clear_cache


@pytest.fixture(autouse=True)
def _clear_model_cache():
    """The in-memory model LRU persists across tests in one process."""
    clear_cache()


def _make_config(tmp_path: Path) -> Config:
    project = tmp_path / "proj"
    (project / "rtl").mkdir(parents=True)
    (project / "tb").mkdir()
    (project / "run.py").write_text("print('run')\n", encoding="utf-8")
    (project / "rtl" / "pkg.vhd").write_text(
        "package pkg is end pkg;\n", encoding="utf-8"
    )
    (project / "tb" / "t_a.vhd").write_text(
        "entity t_a is end t_a;\n", encoding="utf-8"
    )
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


_EXEC_MARKER = {"executed": False}


def _hostile_history():
    """Attacker payload. Module-level so pickle can resolve it by name,
    as an attacker's module would be."""
    _EXEC_MARKER["executed"] = True
    return {}


def _pickle_reduce(func) -> bytes:
    """Pickle bytes that *call* ``func`` on loads (REDUCE) — the code
    execution an attacker gets from a planted database entry."""

    class _Payload:
        def __reduce__(self):
            return (func, ())

    return pickle.dumps(_Payload(), protocol=pickle.HIGHEST_PROTOCOL)


def _write_db_node(db_dir: Path, key: bytes, data: bytes) -> None:
    """VUnit's DataBase node format (vunit/database.py): 4-byte key
    length, key, then the data. Node file names must be numeric."""
    db_dir.mkdir(parents=True, exist_ok=True)
    n = next(i for i in range(1000) if not (db_dir / str(i)).exists())
    (db_dir / str(n)).write_bytes(struct.pack("I", len(key)) + key + data)


def test_load_wipes_hostile_project_database(tmp_path):
    """A hostile project can pre-create the model scratch dir with a
    project_database whose version node matches the running VUnit (the
    dir key is a sha256 of the export content, which it can compute).
    Without the wipe, VUnit reuses the database and pickle.loads runs the
    attacker's entries in the server process."""
    cfg = _make_config(tmp_path)
    export = _hostile_export()
    db = (
        cfg.project_dir
        / ".vunit-mcp-cache"
        / "model"
        / _export_key(export)
        / "project_database"
    )
    # Version node in VUnit's exact format (raw bytes, compared raw by
    # _create_database), so the planted database is reused, not recreated.
    _write_db_node(db, b"version", str((11, sys.version)).encode())
    _write_db_node(db, b"test_history", _pickle_reduce(_hostile_history))

    project, reused = InternalProject.load(cfg, export)
    assert reused is False
    # The planted database must have been discarded: forcing the read
    # that would execute the attacker's pickle proves the entry is gone.
    assert _EXEC_MARKER["executed"] is False
    assert project._vu._get_test_history(None) == {}
    assert _EXEC_MARKER["executed"] is False
    # VUnit recreated a fresh database.
    assert any(p.is_file() for p in db.iterdir())
