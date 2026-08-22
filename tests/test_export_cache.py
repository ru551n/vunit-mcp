"""Tests for the --export-json disk cache (no vunit-hdl/simulator needed).

The fake run.py is a plain Python script emulating `run.py --export-json
<path>`: it writes a fixed export and appends to invocations.log, so tests
can assert the cache actually prevents re-exports.
"""

import os
import sys
from pathlib import Path

from vunit_mcp import export_cache
from vunit_mcp.config import Config

FAKE_RUN_PY = """\
import json, os, sys
args = sys.argv[1:]
if "--export-json" in args:
    out = args[args.index("--export-json") + 1]
    with open("invocations.log", "a") as f:
        f.write("1\\n")
    data = {
        "files": [
            {"file_name": "a.vhd", "file_type": "vhdl",
             "library_name": "work", "attributes": []},
            {"file_name": "tb/t_a.vhd", "file_type": "vhdl",
             "library_name": "tb", "attributes": []},
            # same file again, absolute — exercises the dedupe/absolute path
            {"file_name": os.path.abspath("a.vhd"), "file_type": "vhdl",
             "library_name": "work", "attributes": []},
        ],
        "tests": [
            {"name": "tb.t_a.test1",
             "location": {"file_name": "tb/t_a.vhd", "line_number": 3},
             "attributes": []},
        ],
    }
    with open(out, "w") as f:
        json.dump(data, f)
print("exported")
"""


def make_config(
    tmp_path: Path,
    extra_args: list[str] | None = None,
    fingerprint_exclude: list[str] | None = None,
) -> Config:
    project = tmp_path / "proj"
    (project / "tb").mkdir(parents=True)
    (project / "run.py").write_text(FAKE_RUN_PY, encoding="utf-8")
    (project / "a.vhd").write_text("entity a is end;\n", encoding="utf-8")
    (project / "tb" / "t_a.vhd").write_text("entity t_a is end;\n", encoding="utf-8")
    return Config(
        project_dir=project,
        run_script=project / "run.py",
        python=sys.executable,
        simulator=None,
        output_dir=project / "vunit_out",
        timeout=30.0,
        extra_args=extra_args if extra_args is not None else [],
        fingerprint_exclude=fingerprint_exclude if fingerprint_exclude is not None else [],
    )


def _files() -> list[dict]:
    return [{"file_name": "a.vhd"}, {"file_name": "tb/t_a.vhd"}]


def _files_with_mem() -> list[dict]:
    return _files() + [{"file_name": "mem/rom.hex"}]


def _make_mem_file(cfg: Config) -> Path:
    mem = cfg.project_dir / "mem"
    mem.mkdir(exist_ok=True)
    rom = mem / "rom.hex"
    rom.write_text("data", encoding="utf-8")
    return rom


# -- fingerprint -----------------------------------------------------------


def test_fingerprint_stable_for_unchanged_project(tmp_path):
    cfg = make_config(tmp_path)
    fp = export_cache.fingerprint(cfg, _files())
    assert fp == export_cache.fingerprint(cfg, _files())


def test_fingerprint_changes_on_content_edit(tmp_path):
    cfg = make_config(tmp_path)
    fp = export_cache.fingerprint(cfg, _files())
    (cfg.project_dir / "a.vhd").write_text(
        "entity a is end;\n-- edited\n", encoding="utf-8"
    )
    assert export_cache.fingerprint(cfg, _files()) != fp


def test_fingerprint_changes_on_mtime_only(tmp_path):
    cfg = make_config(tmp_path)
    fp = export_cache.fingerprint(cfg, _files())
    p = cfg.project_dir / "a.vhd"
    st = p.stat()
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    assert export_cache.fingerprint(cfg, _files()) != fp


def test_fingerprint_changes_when_file_disappears(tmp_path):
    cfg = make_config(tmp_path)
    fp = export_cache.fingerprint(cfg, _files())
    (cfg.project_dir / "tb" / "t_a.vhd").unlink()
    assert export_cache.fingerprint(cfg, _files()) != fp


def test_fingerprint_changes_when_run_py_changes(tmp_path):
    cfg = make_config(tmp_path)
    fp = export_cache.fingerprint(cfg, _files())
    (cfg.project_dir / "run.py").write_text(
        FAKE_RUN_PY + "-- added a file\n", encoding="utf-8"
    )
    assert export_cache.fingerprint(cfg, _files()) != fp


def test_fingerprint_changes_with_config(tmp_path):
    cfg = make_config(tmp_path)
    fp = export_cache.fingerprint(cfg, _files())
    other = Config(
        project_dir=cfg.project_dir,
        run_script=cfg.run_script,
        python=cfg.python,
        simulator=None,
        output_dir=cfg.output_dir,
        timeout=cfg.timeout,
        extra_args=["-p", "4"],
    )
    assert export_cache.fingerprint(other, _files()) != fp


# -- fingerprint_exclude -----------------------------------------------------


def test_exclude_ignores_content_change(tmp_path):
    cfg = make_config(tmp_path, fingerprint_exclude=["mem"])
    rom = _make_mem_file(cfg)
    fp = export_cache.fingerprint(cfg, _files_with_mem())
    rom.write_text("changed data\n", encoding="utf-8")
    assert export_cache.fingerprint(cfg, _files_with_mem()) == fp


def test_exclude_ignores_mtime_only(tmp_path):
    cfg = make_config(tmp_path, fingerprint_exclude=["*.hex"])
    rom = _make_mem_file(cfg)
    fp = export_cache.fingerprint(cfg, _files_with_mem())
    st = rom.stat()
    os.utime(rom, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    assert export_cache.fingerprint(cfg, _files_with_mem()) == fp


def test_exclude_path_glob(tmp_path):
    cfg = make_config(tmp_path, fingerprint_exclude=["mem/*"])
    rom = _make_mem_file(cfg)
    fp = export_cache.fingerprint(cfg, _files_with_mem())
    rom.write_text("changed data\n", encoding="utf-8")
    assert export_cache.fingerprint(cfg, _files_with_mem()) == fp


def test_exclude_still_tracks_removal(tmp_path):
    cfg = make_config(tmp_path, fingerprint_exclude=["*.hex"])
    rom = _make_mem_file(cfg)
    fp = export_cache.fingerprint(cfg, _files_with_mem())
    rom.unlink()
    assert export_cache.fingerprint(cfg, _files_with_mem()) != fp


def test_exclude_still_tracks_other_files(tmp_path):
    cfg = make_config(tmp_path, fingerprint_exclude=["mem"])
    _make_mem_file(cfg)
    fp = export_cache.fingerprint(cfg, _files_with_mem())
    (cfg.project_dir / "a.vhd").write_text(
        "entity a is end;\n-- edited\n", encoding="utf-8"
    )
    assert export_cache.fingerprint(cfg, _files_with_mem()) != fp


def test_exclude_empty_keeps_old_behavior(tmp_path):
    cfg = make_config(tmp_path)
    rom = _make_mem_file(cfg)
    fp = export_cache.fingerprint(cfg, _files_with_mem())
    rom.write_text("changed data\n", encoding="utf-8")
    assert export_cache.fingerprint(cfg, _files_with_mem()) != fp


# -- cache file roundtrip ----------------------------------------------------


def test_save_and_load_roundtrip(tmp_path):
    cfg = make_config(tmp_path)
    data = {"files": _files(), "tests": []}
    path = export_cache.save_cached(cfg, data, "fp-1")
    assert path == export_cache.cache_path(cfg)
    assert export_cache.load_cached(cfg) == (data, "fp-1")


def test_load_cached_missing_or_corrupt_returns_none(tmp_path):
    cfg = make_config(tmp_path)
    assert export_cache.load_cached(cfg) is None
    path = export_cache.save_cached(cfg, {"files": []}, "fp-1")
    path.write_text("{not json", encoding="utf-8")
    assert export_cache.load_cached(cfg) is None


# -- end-to-end via a fake run.py ------------------------------------------


async def test_get_export_json_caches_until_sources_change(tmp_path):
    cfg = make_config(tmp_path)
    invocations = cfg.project_dir / "invocations.log"

    o1 = await export_cache.get_export_json(cfg)
    assert o1.error is None
    assert o1.reused is False
    assert o1.data["tests"] == [
        {
            "name": "tb.t_a.test1",
            "location": {"file_name": "tb/t_a.vhd", "line_number": 3},
            "attributes": [],
        }
    ]
    assert invocations.read_text().count("\n") == 1
    assert export_cache.cache_path(cfg).is_file()

    # Unchanged project: served from cache, run.py not re-invoked.
    o2 = await export_cache.get_export_json(cfg)
    assert o2.reused is True
    assert o2.data == o1.data
    assert invocations.read_text().count("\n") == 1

    # Edit a registered source: fingerprint changes, export re-runs.
    f = cfg.project_dir / "tb" / "t_a.vhd"
    f.write_text(f.read_text(encoding="utf-8") + "-- edited\n", encoding="utf-8")
    o3 = await export_cache.get_export_json(cfg)
    assert o3.reused is False
    assert invocations.read_text().count("\n") == 2

    # Cache now matches the current project state.
    data, fp = export_cache.load_cached(cfg)
    assert data is not None
    assert export_cache.fingerprint(cfg, data["files"]) == fp


async def test_get_export_json_run_failure(tmp_path):
    cfg = make_config(tmp_path)
    (cfg.project_dir / "run.py").write_text("import sys; sys.exit(3)\n")
    o = await export_cache.get_export_json(cfg)
    assert o.data is None
    assert o.error is not None and "exit 3" in o.error
    assert not export_cache.cache_path(cfg).exists()


async def test_get_export_json_invalid_json(tmp_path):
    cfg = make_config(tmp_path)
    (cfg.project_dir / "run.py").write_text(
        'import sys\nopen(sys.argv[2], "w").write("not json")\n'
    )
    o = await export_cache.get_export_json(cfg)
    assert o.data is None
    assert o.error is not None and "invalid JSON" in o.error
    assert not export_cache.cache_path(cfg).exists()
