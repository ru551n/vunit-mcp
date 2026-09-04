"""Environment-bound configuration for the VUnit MCP server.

A single VUnit project is addressed via environment variables, read once at
startup. The server shells out to the project's own run.py, so nothing about
the project layout needs to be known in-process.
"""

from __future__ import annotations

import os
import shlex
import shutil
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
    fingerprint_exclude: list[str] = field(default_factory=list)

    @property
    def default_junit_path(self) -> Path:
        return self.output_dir / "junit.xml"


def _venv_bin_dir_name() -> str:
    """The platform-specific scripts subdir name inside a virtualenv."""
    return "Scripts" if os.name == "nt" else "bin"


def _own_venv_bin() -> str | None:
    """This server's own virtualenv 'bin'/'Scripts' dir, if running from one."""
    venv = os.environ.get("VIRTUAL_ENV")
    return str(Path(venv) / _venv_bin_dir_name()) if venv else None


def _resolve_python(project_dir: Path) -> str:
    """Pick the interpreter that shall run the *target* project's run.py.

    The server itself is commonly launched from its own virtualenv (e.g.
    ``uv run vunit-mcp``), which has no reason to contain the target
    project's dependencies (tsfpga, vunit, ...) -- using ``sys.executable``
    unconditionally here reproduces that exact mismatch as a
    ``ModuleNotFoundError`` in the subprocess. Preference order:

    1. A virtualenv inside the project itself (``.venv``/``venv``), the
       common per-project convention.
    2. Otherwise, whatever ``python3``/``python`` a plain shell *in the
       project* would find on PATH -- explicitly excluding this server's
       own virtualenv's ``bin`` dir, so a bare interpreter resolves the
       way the user's own shell would, not to whichever venv happens to
       be running this MCP server.
    3. ``sys.executable`` only as a last resort (e.g. running the test
       suite with no system interpreter reachable another way).

    Always overridable via ``VUNIT_MCP_PYTHON``.
    """
    bin_dir_name = _venv_bin_dir_name()
    exe_names = (
        ("python.exe", "python3.exe") if os.name == "nt" else ("python3", "python")
    )
    for venv_name in (".venv", "venv"):
        for exe_name in exe_names:
            candidate = project_dir / venv_name / bin_dir_name / exe_name
            if candidate.is_file():
                return str(candidate)

    own_bin = _own_venv_bin()
    path_entries = [
        entry
        for entry in os.environ.get("PATH", "").split(os.pathsep)
        if entry and entry != own_bin
    ]
    sanitized_path = os.pathsep.join(path_entries)
    for exe_name in ("python3", "python"):
        found = shutil.which(exe_name, path=sanitized_path)
        if found:
            return found

    return sys.executable


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

    python = os.environ.get("VUNIT_MCP_PYTHON") or _resolve_python(project_dir)
    output_dir_env = os.environ.get("VUNIT_MCP_OUTPUT_DIR")
    if output_dir_env:
        output_dir = Path(output_dir_env).expanduser()
        if not output_dir.is_absolute():
            # Relative against the project dir, not the server's cwd (which
            # is wherever the MCP host launched us).
            output_dir = project_dir / output_dir
        output_dir = output_dir.resolve()
    else:
        output_dir = project_dir / "vunit_out"

    timeout_env = os.environ.get("VUNIT_MCP_TIMEOUT")
    if timeout_env is None:
        timeout = 600.0
    else:
        try:
            timeout = float(timeout_env)
        except ValueError as exc:
            raise ConfigError(
                f"VUNIT_MCP_TIMEOUT must be a number of seconds, got {timeout_env!r}"
            ) from exc
        if timeout <= 0:
            raise ConfigError(f"VUNIT_MCP_TIMEOUT must be positive, got {timeout}")

    extra_args_env = os.environ.get("VUNIT_MCP_EXTRA_ARGS")
    extra_args = shlex.split(extra_args_env) if extra_args_env else []

    exclude_env = os.environ.get("VUNIT_MCP_FINGERPRINT_EXCLUDE", "")
    fingerprint_exclude = [p.strip() for p in exclude_env.split(",") if p.strip()]

    return Config(
        project_dir=project_dir,
        run_script=run_script,
        python=python,
        simulator=os.environ.get("VUNIT_MCP_SIMULATOR") or None,
        output_dir=output_dir,
        timeout=timeout,
        extra_args=extra_args,
        fingerprint_exclude=fingerprint_exclude,
    )


def effective_simulator(config: Config, simulator: str | None = None) -> str | None:
    """Simulator a run.py subprocess will actually use.

    ``simulator`` (a per-call override) wins; then ``VUNIT_MCP_SIMULATOR``
    (``config.simulator``); then ``VUNIT_SIMULATOR`` from the environment
    (inherited by the subprocess); else None — VUnit auto-detects from
    PATH, so we cannot name it here.
    """
    return simulator or config.simulator or os.environ.get("VUNIT_SIMULATOR") or None
