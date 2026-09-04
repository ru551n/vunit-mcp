"""Async subprocess orchestration for invoking the project's run.py.

VUnit has no standalone CLI and VUnit.main() calls sys.exit(), so the server
never imports vunit itself here (project_model is the deliberate exception).
Every operation is a subprocess:
    <python> <run.py> <args...>
— exactly how a human runs VUnit from a terminal.
"""

from __future__ import annotations

import asyncio
import os
import signal
from dataclasses import dataclass
from pathlib import Path

from .config import Config
from .parsing import strip_ansi


class RunTimeoutError(RuntimeError):
    """Raised when a run.py invocation exceeds its timeout."""


def _kill_process_group(proc: asyncio.subprocess.Process) -> None:
    """Kill a subprocess and everything it spawned (e.g. the simulator).

    On POSIX, kill the whole process group (run.py's children, like the
    simulator, share it via start_new_session). Windows has no process
    groups; fall back to killing the direct child.
    """
    if os.name == "posix":
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            return
        except (ProcessLookupError, PermissionError):
            pass
    proc.kill()


@dataclass(frozen=True)
class RunResult:
    returncode: int
    stdout: str
    stderr: str
    argv: list[str]

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def full_text(self) -> str:
        """stdout + stderr, untruncated.

        ``summary()`` keeps the TAIL (where VUnit's results live); failure
        diagnostics such as analyzer errors appear at the HEAD, so error
        extraction over a summary can miss them. Use ``full_text`` when
        hunting errors anywhere in the output."""
        parts = [p for p in (self.stdout.strip(), self.stderr.strip()) if p]
        return "\n".join(parts) if parts else "(no output)"

    def summary(self, max_chars: int = 4000) -> str:
        """Agent-readable text: stdout, then stderr if any, size-bounded.

        Truncation keeps the TAIL, not the head: failures, compile errors
        and VUnit's result lines appear at the end of the output, which is
        what an agent needs to diagnose a problem.
        """
        parts = []
        if self.stdout:
            parts.append(self.stdout.strip())
        if self.stderr:
            err = self.stderr.strip()
            # vunit prints its results to stderr for some operations; surface it
            parts.append(f"--- stderr ---\n{err}")
        text = "\n".join(parts) if parts else "(no output)"
        if len(text) > max_chars:
            text = (
                f"… [truncated: showing last {max_chars} of {len(text)} chars]\n"
                + text[-max_chars:]
            )
        return text


def build_argv(config: Config, args: list[str]) -> list[str]:
    return [
        config.python,
        str(config.run_script),
        *args,
        *config.extra_args,
    ]


def run_env(config: Config, simulator: str | None = None) -> dict[str, str]:
    """Environment for the run.py subprocess.

    ``simulator`` (a per-call override) wins over the server-level
    ``VUNIT_MCP_SIMULATOR`` (``config.simulator``). When neither is set,
    ``VUNIT_SIMULATOR`` from the environment (if any) and VUnit's own
    PATH auto-detection decide.

    This server may itself be running from its own virtualenv (e.g. via
    ``uv run vunit-mcp``); that venv describes *this* process, not the
    target project, so its ``VIRTUAL_ENV``/``PYTHONHOME`` and its ``bin``
    dir on PATH are stripped here rather than handed to the subprocess
    (``config.python`` is resolved separately, see ``config._resolve_python``
    — this only prevents the target's own subprocesses, e.g. simulator
    invocations made from run.py, from picking the wrong interpreter).
    """
    env = dict(os.environ)
    own_venv = env.pop("VIRTUAL_ENV", None)
    env.pop("PYTHONHOME", None)
    if own_venv:
        own_bin = str(Path(own_venv) / "bin")
        env["PATH"] = os.pathsep.join(
            entry for entry in env.get("PATH", "").split(os.pathsep) if entry != own_bin
        )
    sim = simulator or config.simulator
    if sim:
        env["VUNIT_SIMULATOR"] = sim
    return env


async def run_vunit(
    config: Config,
    args: list[str],
    *,
    timeout: float | None = None,
    simulator: str | None = None,
) -> RunResult:
    """Run <python> <run.py> <args> and capture output. Raises on timeout.

    ``simulator`` overrides the server-level simulator for this call only.
    Always the project's own run.py — never a generated one (the export
    model is lossy; see project_model)."""
    argv = build_argv(config, args)
    limit = timeout if timeout is not None else config.timeout
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(config.project_dir),
        env=run_env(config, simulator),
        # Own process group so a timeout can kill the simulator children
        # that run.py spawns, not just run.py itself.
        start_new_session=True,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=limit)
    except asyncio.TimeoutError:
        _kill_process_group(proc)
        await proc.wait()
        raise RunTimeoutError(
            f"Timed out after {limit:.0f}s: {' '.join(argv)}"
        ) from None
    return RunResult(
        returncode=proc.returncode or 0,
        stdout=strip_ansi(out.decode(errors="replace")),
        stderr=strip_ansi(err.decode(errors="replace")),
        argv=argv,
    )


def run_subprocess_sync(
    config: Config, argv: list[str], *, timeout: float | None = None
) -> RunResult:
    """Small helper for quick non-vunit commands (e.g. version probes)."""
    import subprocess

    limit = timeout if timeout is not None else 30.0
    try:
        proc = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(config.project_dir),
            # Own process group: on timeout we kill the group, not just the
            # direct child (mirrors run_vunit).
            start_new_session=True,
        )
    except FileNotFoundError as exc:
        raise RunTimeoutError(f"Interpreter not found: {argv[0]} ({exc})") from exc
    try:
        out, err = proc.communicate(timeout=limit)
    except subprocess.TimeoutExpired as timeout_exc:
        if os.name == "posix":
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                proc.kill()
        else:
            proc.kill()
        proc.wait()
        raise RunTimeoutError(
            f"Timed out after {limit:.0f}s: {' '.join(argv)}"
        ) from timeout_exc
    return RunResult(
        returncode=proc.returncode,
        stdout=strip_ansi(out or ""),
        stderr=strip_ansi(err or ""),
        argv=argv,
    )


async def resolve_junit_path(output_dir: Path) -> Path | None:
    """Locate the latest junit XML in an output dir (junit.xml, or the
    newest junit-named XML at the top level / in test_output/).

    Only those two levels are scanned — the report is never nested deeper,
    and a recursive glob over a big output dir (thousands of per-test
    dirs) would be slow for no benefit.
    """
    direct = output_dir / "junit.xml"
    if direct.is_file():
        return direct
    xmls: list[Path] = []
    for level in (output_dir, output_dir / "test_output"):
        if level.is_dir():
            xmls.extend(p for p in level.glob("*.xml") if "junit" in p.name.lower())
    if not xmls:
        return None
    return max(xmls, key=lambda p: p.stat().st_mtime)
