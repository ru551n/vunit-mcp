"""Tests for env-var configuration (vunit_mcp.config)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from vunit_mcp.config import (
    Config,
    ConfigError,
    _resolve_python,
    effective_simulator,
    load_config,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """No VUNIT_MCP_* env may leak in from the developer's environment."""
    for key in list(os.environ):
        if key.startswith("VUNIT_MCP_"):
            monkeypatch.delenv(key, raising=False)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    d = tmp_path / "proj"
    d.mkdir()
    (d / "run.py").write_text("", encoding="utf-8")
    return d


def test_project_dir_defaults_to_cwd(monkeypatch, project):
    monkeypatch.chdir(project)
    cfg = load_config()
    assert cfg.project_dir == project.resolve()
    assert cfg.run_script == project / "run.py"


def test_run_script_defaults_to_simulate_py_if_no_run_py(monkeypatch, tmp_path):
    d = tmp_path / "sim_proj"
    d.mkdir()
    (d / "simulate.py").write_text("", encoding="utf-8")
    monkeypatch.setenv("VUNIT_MCP_PROJECT_DIR", str(d))
    cfg = load_config()
    assert cfg.run_script == d / "simulate.py"


def test_run_script_prefers_run_py_over_simulate_py(monkeypatch, tmp_path):
    d = tmp_path / "both_proj"
    d.mkdir()
    (d / "run.py").write_text("", encoding="utf-8")
    (d / "simulate.py").write_text("", encoding="utf-8")
    monkeypatch.setenv("VUNIT_MCP_PROJECT_DIR", str(d))
    cfg = load_config()
    assert cfg.run_script == d / "run.py"


def test_no_run_script_found(monkeypatch, tmp_path):
    d = tmp_path / "empty_proj"
    d.mkdir()
    monkeypatch.setenv("VUNIT_MCP_PROJECT_DIR", str(d))
    with pytest.raises(ConfigError, match="Run script not found"):
        load_config()


def test_project_dir_not_a_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("VUNIT_MCP_PROJECT_DIR", str(tmp_path / "nope"))
    with pytest.raises(ConfigError, match="not a directory"):
        load_config()


def test_run_script_missing(monkeypatch, project):
    (project / "run.py").unlink()
    monkeypatch.setenv("VUNIT_MCP_PROJECT_DIR", str(project))
    with pytest.raises(ConfigError, match="Run script not found"):
        load_config()


def test_defaults(monkeypatch, project):
    monkeypatch.setenv("VUNIT_MCP_PROJECT_DIR", str(project))
    cfg = load_config()
    assert cfg.run_script == project / "run.py"
    # No project venv and no override: falls back to a PATH lookup, not
    # unconditionally to this server's own interpreter (see test_resolve_python).
    assert cfg.python
    assert cfg.output_dir == project / "vunit_out"
    assert cfg.timeout == 600.0
    assert cfg.simulator is None
    assert cfg.extra_args == []
    assert cfg.fingerprint_exclude == []


def test_env_overrides(monkeypatch, project):
    (project / "custom").mkdir()
    (project / "custom" / "run.py").write_text("", encoding="utf-8")
    monkeypatch.setenv("VUNIT_MCP_PROJECT_DIR", str(project))
    monkeypatch.setenv("VUNIT_MCP_RUN_SCRIPT", "custom/run.py")
    monkeypatch.setenv("VUNIT_MCP_PYTHON", "/usr/bin/python3")
    monkeypatch.setenv("VUNIT_MCP_SIMULATOR", "ghdl")
    monkeypatch.setenv("VUNIT_MCP_TIMEOUT", "42")
    monkeypatch.setenv("VUNIT_MCP_EXTRA_ARGS", "--clean -p 4")
    monkeypatch.setenv("VUNIT_MCP_FINGERPRINT_EXCLUDE", "*.hex,mem")
    cfg = load_config()
    assert cfg.run_script == project / "custom" / "run.py"
    assert cfg.python == "/usr/bin/python3"
    assert cfg.simulator == "ghdl"
    assert cfg.timeout == 42.0
    assert cfg.extra_args == ["--clean", "-p", "4"]
    assert cfg.fingerprint_exclude == ["*.hex", "mem"]


def test_relative_output_dir_resolves_vs_project(monkeypatch, project):
    monkeypatch.setenv("VUNIT_MCP_PROJECT_DIR", str(project))
    monkeypatch.setenv("VUNIT_MCP_OUTPUT_DIR", "build/out")
    cfg = load_config()
    assert cfg.output_dir == (project / "build" / "out").resolve()


def test_absolute_output_dir(monkeypatch, project, tmp_path):
    monkeypatch.setenv("VUNIT_MCP_PROJECT_DIR", str(project))
    monkeypatch.setenv("VUNIT_MCP_OUTPUT_DIR", str(tmp_path / "elsewhere"))
    cfg = load_config()
    assert cfg.output_dir == (tmp_path / "elsewhere").resolve()


@pytest.mark.parametrize("value", ["abc", "0", "-5"])
def test_invalid_timeout(monkeypatch, project, value):
    monkeypatch.setenv("VUNIT_MCP_PROJECT_DIR", str(project))
    monkeypatch.setenv("VUNIT_MCP_TIMEOUT", value)
    with pytest.raises(ConfigError, match="VUNIT_MCP_TIMEOUT"):
        load_config()


# --- effective_simulator -----------------------------------------------------


def _cfg(simulator: str | None) -> Config:
    return Config(
        project_dir=Path("/p"),
        run_script=Path("/p/run.py"),
        python=sys.executable,
        simulator=simulator,
        output_dir=Path("/p/vunit_out"),
        timeout=600.0,
    )


def test_effective_simulator_mcp_wins(monkeypatch):
    monkeypatch.setenv("VUNIT_SIMULATOR", "nvc")
    assert effective_simulator(_cfg("ghdl")) == "ghdl"


def test_effective_simulator_falls_back_to_vunit_env(monkeypatch):
    monkeypatch.setenv("VUNIT_SIMULATOR", "nvc")
    assert effective_simulator(_cfg(None)) == "nvc"


def test_effective_simulator_none(monkeypatch):
    assert effective_simulator(_cfg(None)) is None


# --- _resolve_python ----------------------------------------------------------


def test_resolve_python_prefers_project_dot_venv(tmp_path, monkeypatch):
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    bin_dir_name = "Scripts" if os.name == "nt" else "bin"
    exe_name = "python.exe" if os.name == "nt" else "python3"
    venv_bin = tmp_path / ".venv" / bin_dir_name
    venv_bin.mkdir(parents=True)
    python3 = venv_bin / exe_name
    python3.write_text("", encoding="utf-8")
    assert _resolve_python(tmp_path) == str(python3)


def test_resolve_python_prefers_project_venv_over_venv_name(tmp_path, monkeypatch):
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    bin_dir_name = "Scripts" if os.name == "nt" else "bin"
    exe_name = "python3.exe" if os.name == "nt" else "python"
    venv_bin = tmp_path / "venv" / bin_dir_name
    venv_bin.mkdir(parents=True)
    python_exe = venv_bin / exe_name
    python_exe.write_text("", encoding="utf-8")
    assert _resolve_python(tmp_path) == str(python_exe)


def test_resolve_python_excludes_own_virtualenv_from_path(tmp_path, monkeypatch):
    """No project venv: falls back to PATH, but must skip this server's own
    virtualenv bin dir (VIRTUAL_ENV) so it doesn't reproduce the exact bug
    (running the target under the *server's* environment) via the back door."""
    exe_name = "python.exe" if os.name == "nt" else "python3"

    own_venv = tmp_path / "server_venv"
    own_bin = own_venv / ("Scripts" if os.name == "nt" else "bin")
    own_bin.mkdir(parents=True)
    (own_bin / exe_name).write_text("", encoding="utf-8")
    (own_bin / exe_name).chmod(0o755)

    real_bin = tmp_path / "usr_bin"
    real_bin.mkdir()
    real_python = real_bin / exe_name
    real_python.write_text("", encoding="utf-8")
    real_python.chmod(0o755)

    monkeypatch.setenv("VIRTUAL_ENV", str(own_venv))
    monkeypatch.setenv("PATH", f"{own_bin}{os.pathsep}{real_bin}")

    project_dir = tmp_path / "proj_no_venv"
    project_dir.mkdir()
    assert _resolve_python(project_dir) == str(real_python)


def test_resolve_python_falls_back_to_sys_executable(tmp_path, monkeypatch):
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    project_dir = tmp_path / "proj_no_venv"
    project_dir.mkdir()
    assert _resolve_python(project_dir) == sys.executable
