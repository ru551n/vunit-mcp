"""Unit tests for RunResult.summary truncation (no simulator needed)."""

import os
import sys
from pathlib import Path

from vunit_mcp.config import Config
from vunit_mcp.runner import RunResult, run_env


def _cfg(**overrides) -> Config:
    defaults = {
        "project_dir": Path("/p"),
        "run_script": Path("/p/run.py"),
        "python": sys.executable,
        "simulator": None,
        "output_dir": Path("/p/vunit_out"),
        "timeout": 600.0,
    }
    defaults.update(overrides)
    return Config(**defaults)


def test_run_env_strips_own_virtualenv(tmp_path, monkeypatch):
    own_venv = tmp_path / "server_venv"
    own_bin = own_venv / "bin"
    own_bin.mkdir(parents=True)
    monkeypatch.setenv("VIRTUAL_ENV", str(own_venv))
    monkeypatch.setenv("PYTHONHOME", "/should/be/removed")
    monkeypatch.setenv("PATH", f"{own_bin}{os.pathsep}/usr/bin")

    env = run_env(_cfg())

    assert "VIRTUAL_ENV" not in env
    assert "PYTHONHOME" not in env
    assert str(own_bin) not in env["PATH"].split(os.pathsep)
    assert "/usr/bin" in env["PATH"].split(os.pathsep)


def test_run_env_sets_simulator_override(monkeypatch):
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    env = run_env(_cfg(simulator="ghdl"))
    assert env["VUNIT_SIMULATOR"] == "ghdl"


def test_summary_short_untouched():
    r = RunResult(returncode=0, stdout="ok", stderr="", argv=[])
    assert r.summary() == "ok"


def test_summary_tail_truncation_keeps_the_end():
    r = RunResult(
        returncode=1,
        stdout="\n".join(f"line {i}" for i in range(1000)),
        stderr="final error line",
        argv=[],
    )
    text = r.summary(max_chars=500)
    assert text.endswith("final error line")
    assert "line 999" in text
    assert "line 0" not in text
    assert "truncated" in text
    assert len(text) <= 500 + 100  # marker line overhead


def test_summary_stderr_included():
    r = RunResult(returncode=1, stdout="out", stderr="err", argv=[])
    text = r.summary()
    assert "out" in text and "err" in text
