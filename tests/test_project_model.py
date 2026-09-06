"""Tests for the project model behind vunit_test_dependencies.

The model answers name lookups from the export alone, and delegates the one
question that needs VUnit's internal API to ``dependency_probe.py``, run by
the *project's* interpreter. These tests cover the client half: what it
sends, what it does with the reply, its caching, and the scratch-dir wipe
that stops a hostile project from planting a pickled database there. The
real round trip through VUnit lives in the e2e test at the bottom, which
skips unless vunit-hdl is installed.
"""

import json
import pickle
import struct
import subprocess
import sys
from pathlib import Path

import pytest

from vunit_mcp.config import Config
from vunit_mcp.project_model import (
    InternalProject,
    InternalProjectError,
    _export_key,
    clear_cache,
)


@pytest.fixture(autouse=True)
def _clear_model_cache():
    """The answer LRU and the trusted-scratch set persist across tests in
    one process."""
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


def _export() -> dict:
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


def _scratch(cfg: Config, export: dict) -> Path:
    return cfg.project_dir / ".vunit-mcp-cache" / "model" / _export_key(export)


def _fake_probe(monkeypatch, subset, *, calls=None, returncode=0):
    """Stand in for the probe subprocess, writing the reply it would write."""

    def _run(argv, *, input, **kwargs):
        request = json.loads(input)
        if calls is not None:
            calls.append((argv, request, kwargs))
        if returncode == 0:
            Path(request["scratch"], "result.json").write_text(
                json.dumps({"subset": subset}), encoding="utf-8"
            )
        return subprocess.CompletedProcess(argv, returncode, stdout="", stderr="boom")

    monkeypatch.setattr(subprocess, "run", _run)


# --- what the client sends ---------------------------------------------------


def test_probe_runs_the_project_interpreter_in_the_project_dir(tmp_path, monkeypatch):
    """The probe must be the project's interpreter, started in the project
    dir with the project venv activated -- that is the whole point of not
    importing vunit here."""
    cfg = _make_config(tmp_path)
    foreign = tmp_path / "elsewhere"
    foreign.mkdir()
    monkeypatch.chdir(foreign)
    calls = []
    _fake_probe(monkeypatch, [["tb", "/abs/t_a.vhd"]], calls=calls)

    project = InternalProject.load(cfg, _export())
    project.implementation_subset(project.resolve_test("tb.t_a.test1")[0])

    argv, request, kwargs = calls[0]
    assert argv[0] == cfg.python
    assert Path(argv[1]).name == "dependency_probe.py"
    assert kwargs["cwd"] == str(cfg.project_dir)
    # Export file names are relative to the project dir, not the server's
    # cwd (wherever the MCP host launched it) -- the probe is told which.
    assert request["project_dir"] == str(cfg.project_dir)
    assert request["test_file"] == "tb/t_a.vhd"
    assert [f["file_name"] for f in request["files"]] == ["rtl/pkg.vhd", "tb/t_a.vhd"]


def test_probe_failure_is_reported_with_the_interpreter(tmp_path, monkeypatch):
    cfg = _make_config(tmp_path)
    _fake_probe(monkeypatch, [], returncode=1)
    project = InternalProject.load(cfg, _export())
    with pytest.raises(InternalProjectError) as excinfo:
        project.implementation_subset(project.resolve_test("tb.t_a.test1")[0])
    assert cfg.python in str(excinfo.value)
    assert "boom" in str(excinfo.value)


def test_missing_vunit_is_reported_as_a_project_venv_problem(tmp_path, monkeypatch):
    """The likeliest failure now that vunit-mcp does not ship VUnit: the
    project venv has none either. Say so, rather than dumping a traceback."""
    cfg = _make_config(tmp_path)

    def _run(argv, *, input, **kwargs):
        return subprocess.CompletedProcess(
            argv, 1, stdout="", stderr="ModuleNotFoundError: No module named 'vunit'"
        )

    monkeypatch.setattr(subprocess, "run", _run)
    project = InternalProject.load(cfg, _export())
    with pytest.raises(InternalProjectError) as excinfo:
        project.implementation_subset(project.resolve_test("tb.t_a.test1")[0])
    assert "not installed in the project's virtualenv" in str(excinfo.value)


# --- lookups that need no probe ----------------------------------------------


def test_name_lookups_do_not_run_the_probe(tmp_path, monkeypatch):
    cfg = _make_config(tmp_path)

    def _explode(*_a, **_kw):
        raise AssertionError("name lookups must not shell out")

    monkeypatch.setattr(subprocess, "run", _explode)
    project = InternalProject.load(cfg, _export())
    assert project.test_names == ["tb.t_a.test1"]
    assert project.resolve_test("tb.t_a.*")[0]["name"] == "tb.t_a.test1"
    assert project.resolve_test("nope") == []


def test_answers_are_cached_per_test(tmp_path, monkeypatch):
    cfg = _make_config(tmp_path)
    calls = []
    _fake_probe(monkeypatch, [["tb", "/abs/t_a.vhd"]], calls=calls)
    project = InternalProject.load(cfg, _export())
    test = project.resolve_test("tb.t_a.test1")[0]

    first, reused = project.implementation_subset(test)
    assert reused is False
    second, reused = project.implementation_subset(test)
    assert reused is True
    assert first == second == [("tb", "/abs/t_a.vhd")]
    assert len(calls) == 1


# --- the hostile database ----------------------------------------------------


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


def test_first_probe_wipes_a_planted_project_database(tmp_path, monkeypatch):
    """A hostile project can pre-create the model scratch dir with a
    project_database whose version node matches the probe's VUnit (the dir
    key is a sha256 of the export content, which it can compute). Without
    the wipe, VUnit reuses it and pickle.loads runs the attacker's entries.
    """
    cfg = _make_config(tmp_path)
    export = _export()
    db = _scratch(cfg, export) / "project_database"
    # Version node in VUnit's exact format (raw bytes, compared raw by
    # _create_database), so the planted database would be reused, not
    # recreated.
    _write_db_node(db, b"version", str((11, sys.version)).encode())
    _write_db_node(db, b"test_history", _pickle_reduce(_hostile_history))

    _fake_probe(monkeypatch, [["tb", "/abs/t_a.vhd"]])
    project = InternalProject.load(cfg, export)
    project.implementation_subset(project.resolve_test("tb.t_a.test1")[0])

    assert not db.exists()
    assert _EXEC_MARKER["executed"] is False


def test_later_probes_keep_the_database_we_wrote(tmp_path, monkeypatch):
    """The wipe is first-probe-only: the database the probe then writes is
    VUnit's parse cache, and re-parsing every source on each query would
    make the tool unusable on a real project."""
    cfg = _make_config(tmp_path)
    export = _export()
    scratch = _scratch(cfg, export)
    calls = []
    _fake_probe(monkeypatch, [["tb", "/abs/t_a.vhd"]], calls=calls)
    project = InternalProject.load(cfg, export)

    project.implementation_subset(project.resolve_test("tb.t_a.test1")[0])
    # The probe (faked here) would have left a database behind.
    _write_db_node(scratch / "project_database", b"version", b"ours")

    # A different test on the same export: probes again, must not wipe.
    project._files.append(
        {
            "file_name": "tb/t_b.vhd",
            "file_type": "vhdl",
            "library_name": "tb",
            "attributes": [],
        }
    )
    project.implementation_subset(
        {"name": "tb.t_a.test2", "location": export["tests"][0]["location"]}
    )
    assert (scratch / "project_database").exists()
    assert len(calls) == 2


# --- the real round trip -----------------------------------------------------


def test_end_to_end_against_a_real_vunit(tmp_path, monkeypatch):
    """The probe really does answer with VUnit's implementation subset.

    Uses this interpreter as the "project" one, so it needs vunit-hdl:
    `uv sync --group e2e`.
    """
    pytest.importorskip("vunit", reason="requires vunit-hdl (uv sync --group e2e)")
    cfg = _make_config(tmp_path)
    foreign = tmp_path / "elsewhere"
    foreign.mkdir()
    monkeypatch.chdir(foreign)

    project = InternalProject.load(cfg, _export())
    subset, reused = project.implementation_subset(
        project.resolve_test("tb.t_a.test1")[0]
    )
    assert reused is False
    assert ("tb", str((cfg.project_dir / "tb" / "t_a.vhd").resolve())) in subset
