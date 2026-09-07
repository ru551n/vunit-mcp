"""Environment-bound configuration for the VUnit MCP server.

A single VUnit project is addressed via environment variables, read once at
startup. The server shells out to the project's own run.py (or simulate.py),
so nothing about the project layout needs to be known in-process.

``VUNIT_MCP_PROJECT_DIR`` defaults to the server's current working directory,
and ``VUNIT_MCP_RUN_SCRIPT`` to whichever of ``run.py``/``simulate.py`` exists
in it (``run.py`` wins if both do) -- set either explicitly when the server
isn't launched from the project directory, or the run script has a different
name/location.

The project's own virtualenv is resolved here too: an existing
``.venv``/``venv`` is always used (and activated for the subprocess, see
``runner.run_env``), and one is created with uv from the project's
``pyproject.toml``/``requirements.txt`` when it has none -- see
``project_venv``.
"""

from __future__ import annotations

import os
import shlex
import shutil
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from .project_venv import (
    DEFAULT_VENV_TIMEOUT,
    auto_venv_enabled,
    ensure_venv,
    owning_venv,
    venv_bin_dir_name,
)


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
    venv: Path | None = None
    venv_notes: tuple[str, ...] = ()

    @property
    def default_junit_path(self) -> Path:
        return self.output_dir / "junit.xml"


def _own_venv_bin(env: Mapping[str, str]) -> str | None:
    """This server's own virtualenv 'bin'/'Scripts' dir, if running from one."""
    venv = env.get("VIRTUAL_ENV")
    return str(Path(venv) / venv_bin_dir_name()) if venv else None


def _resolve_python(project_dir: Path, env: Mapping[str, str]) -> str:
    """Fallback interpreter for a project that has (and cannot get) no venv.

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

    Always overridable via ``VUNIT_MCP_PYTHON``. Normally unused: a project
    venv is created if missing (``_resolve_venv_and_python``), and its
    interpreter wins -- this is the degraded path (no uv installed, or
    nothing to install from).
    """
    bin_dir_name = venv_bin_dir_name()
    exe_names = (
        ("python.exe", "python3.exe") if os.name == "nt" else ("python3", "python")
    )
    for venv_name in (".venv", "venv"):
        for exe_name in exe_names:
            candidate = project_dir / venv_name / bin_dir_name / exe_name
            if candidate.is_file():
                return str(candidate)

    own_bin = _own_venv_bin(env)
    path_entries = [
        entry
        for entry in env.get("PATH", "").split(os.pathsep)
        if entry and entry != own_bin
    ]
    sanitized_path = os.pathsep.join(path_entries)
    for exe_name in exe_names:
        found = shutil.which(exe_name, path=sanitized_path)
        if found:
            return found

    return sys.executable


_DEFAULT_RUN_SCRIPT_NAMES = ("run.py", "simulate.py")


def _default_run_script(project_dir: Path) -> Path:
    """Whichever of run.py/simulate.py exists in ``project_dir``.

    ``run.py`` wins if both are present. Returns the ``run.py`` path (even if
    absent) when neither exists, so the caller's "not found" error names it.
    """
    for name in _DEFAULT_RUN_SCRIPT_NAMES:
        candidate = project_dir / name
        if candidate.is_file():
            return candidate
    return project_dir / _DEFAULT_RUN_SCRIPT_NAMES[0]


def _venv_timeout(env: Mapping[str, str]) -> float:
    raw = env.get("VUNIT_MCP_VENV_TIMEOUT", "").strip()
    if not raw:
        return DEFAULT_VENV_TIMEOUT
    try:
        timeout = float(raw)
    except ValueError as exc:
        raise ConfigError(
            f"VUNIT_MCP_VENV_TIMEOUT must be a number of seconds, got {raw!r}"
        ) from exc
    if timeout <= 0:
        raise ConfigError(f"VUNIT_MCP_VENV_TIMEOUT must be positive, got {timeout}")
    return timeout


def _resolve_venv_and_python(
    project_dir: Path, env: Mapping[str, str]
) -> tuple[Path | None, str, tuple[str, ...]]:
    """The venv to activate, the interpreter to run, and any setup notes.

    An explicit ``VUNIT_MCP_PYTHON`` is authoritative and never triggers
    provisioning -- but if it points into a virtualenv, that venv is still
    activated for the subprocess rather than merely executed.
    """
    explicit_python = env.get("VUNIT_MCP_PYTHON", "").strip()
    if explicit_python:
        return owning_venv(explicit_python), explicit_python, ()

    result = ensure_venv(
        project_dir,
        create=auto_venv_enabled(env, "VUNIT_MCP_AUTO_VENV"),
        uv=env.get("VUNIT_MCP_UV", "").strip() or None,
        timeout=_venv_timeout(env),
        env=env,
    )
    interpreter = result.interpreter
    if interpreter is None:
        return None, _resolve_python(project_dir, env), result.notes
    return result.venv, interpreter, result.notes


def load_config(env: Mapping[str, str] | None = None) -> Config:
    """Build a :class:`Config` from ``env`` (default: ``os.environ``).

    Raises:
        ConfigError: if ``VUNIT_MCP_PROJECT_DIR`` is not a directory, the
            run script does not exist, or a numeric override is invalid.
    """
    source: Mapping[str, str] = os.environ if env is None else env

    project_dir_env = source.get("VUNIT_MCP_PROJECT_DIR", "").strip()
    project_dir = (
        Path(project_dir_env).expanduser().resolve() if project_dir_env else Path.cwd()
    )
    if not project_dir.is_dir():
        raise ConfigError(f"VUNIT_MCP_PROJECT_DIR is not a directory: {project_dir}")

    run_script_env = source.get("VUNIT_MCP_RUN_SCRIPT", "").strip()
    if run_script_env:
        run_script = (project_dir / run_script_env).resolve()
    else:
        run_script = _default_run_script(project_dir).resolve()

    if not run_script.is_file():
        raise ConfigError(
            f"Run script not found: {run_script}. Set VUNIT_MCP_PROJECT_DIR to "
            "the project directory and/or VUNIT_MCP_RUN_SCRIPT to the run "
            "script's name/path if it isn't run.py or simulate.py in the "
            "current working directory."
        )

    venv, python, venv_notes = _resolve_venv_and_python(project_dir, source)
    output_dir_env = source.get("VUNIT_MCP_OUTPUT_DIR", "").strip()
    if output_dir_env:
        output_dir = Path(output_dir_env).expanduser()
        if not output_dir.is_absolute():
            # Relative against the project dir, not the server's cwd (which
            # is wherever the MCP host launched us).
            output_dir = project_dir / output_dir
        output_dir = output_dir.resolve()
    else:
        output_dir = project_dir / "vunit_out"

    timeout_env = source.get("VUNIT_MCP_TIMEOUT", "").strip()
    if not timeout_env:
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

    extra_args_env = source.get("VUNIT_MCP_EXTRA_ARGS", "")
    extra_args = shlex.split(extra_args_env) if extra_args_env else []

    exclude_env = source.get("VUNIT_MCP_FINGERPRINT_EXCLUDE", "")
    fingerprint_exclude = [p.strip() for p in exclude_env.split(",") if p.strip()]

    return Config(
        project_dir=project_dir,
        run_script=run_script,
        python=python,
        simulator=source.get("VUNIT_MCP_SIMULATOR", "").strip() or None,
        output_dir=output_dir,
        timeout=timeout,
        extra_args=extra_args,
        fingerprint_exclude=fingerprint_exclude,
        venv=venv,
        venv_notes=venv_notes,
    )


def effective_simulator(
    config: Config,
    simulator: str | None = None,
    env: Mapping[str, str] | None = None,
) -> str | None:
    """Simulator a run.py subprocess will actually use.

    ``simulator`` (a per-call override) wins; then ``VUNIT_MCP_SIMULATOR``
    (``config.simulator``); then ``VUNIT_SIMULATOR`` from ``env`` (default:
    ``os.environ``, inherited by the subprocess); else None — VUnit
    auto-detects from PATH, so we cannot name it here.
    """
    source: Mapping[str, str] = os.environ if env is None else env
    return simulator or config.simulator or source.get("VUNIT_SIMULATOR") or None
