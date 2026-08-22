"""Environment-bound configuration for the VUnit MCP server.

A single VUnit project is addressed via environment variables, read once at
startup. The server shells out to the project's own run.py, so nothing about
the project layout needs to be known in-process.
"""

from __future__ import annotations

import os
import shlex
import sys
from dataclasses import dataclass, field
from pathlib import Path


class ConfigError(RuntimeError):
    """Raised when the server cannot be configured/validated."""


@dataclass(frozen=True)
class Config:
    project_dir: Path
    run_script: Path
    python: str
    simulator: str | None
    output_dir: Path
    timeout: float
    extra_args: list[str] = field(default_factory=list)

    @property
    def default_junit_path(self) -> Path:
        return self.output_dir / "junit.xml"


def load_config() -> Config:
    project_dir_env = os.environ.get("VUNIT_MCP_PROJECT_DIR")
    if not project_dir_env:
        raise ConfigError(
            "VUNIT_MCP_PROJECT_DIR is not set. Point it at the directory "
            "containing the VUnit project's run.py."
        )
    project_dir = Path(project_dir_env).expanduser().resolve()
    run_script = (
        project_dir / os.environ.get("VUNIT_MCP_RUN_SCRIPT", "run.py")
    ).resolve()

    if not project_dir.is_dir():
        raise ConfigError(f"VUNIT_MCP_PROJECT_DIR is not a directory: {project_dir}")
    if not run_script.is_file():
        raise ConfigError(
            f"Run script not found: {run_script}. Check VUNIT_MCP_PROJECT_DIR "
            "and VUNIT_MCP_RUN_SCRIPT."
        )

    python = os.environ.get("VUNIT_MCP_PYTHON") or sys.executable
    output_dir_env = os.environ.get("VUNIT_MCP_OUTPUT_DIR")
    output_dir = (
        Path(output_dir_env).expanduser().resolve()
        if output_dir_env
        else project_dir / "vunit_out"
    )
    timeout = float(os.environ.get("VUNIT_MCP_TIMEOUT", "600"))

    extra_args_env = os.environ.get("VUNIT_MCP_EXTRA_ARGS")
    extra_args = shlex.split(extra_args_env) if extra_args_env else []

    return Config(
        project_dir=project_dir,
        run_script=run_script,
        python=python,
        simulator=os.environ.get("VUNIT_MCP_SIMULATOR") or None,
        output_dir=output_dir,
        timeout=timeout,
        extra_args=extra_args,
    )
