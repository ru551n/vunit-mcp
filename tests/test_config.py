"""Tests for env-var configuration (vunit_mcp.config)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from vunit_mcp.config import (
    Config,
    ConfigError,
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


def test_missing_project_dir(monkeypatch):
    with pytest.raises(ConfigError, match="VUNIT_MCP_PROJECT_DIR is not set"):
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
    assert cfg.python == sys.executable
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
