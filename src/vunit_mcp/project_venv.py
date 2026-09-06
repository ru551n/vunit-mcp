"""Locate — and when missing, create — the target project's virtualenv.

The server shells out to the project's own ``run.py``, which needs the
*project's* dependencies (``vunit-hdl``, ``tsfpga``, ...) — never this
server's. So a project virtualenv (``.venv``/``venv``) is always used when
one exists, and is *created with uv* when one does not but the project
declares its dependencies (``pyproject.toml`` or ``requirements.txt``).

Two halves matter, and both are applied:

* the interpreter that runs the script is the venv's own
  (``<venv>/bin/python``), and
* the subprocess environment is *activated* against that venv —
  ``VIRTUAL_ENV`` set, ``<venv>/bin`` prepended to ``PATH``, ``PYTHONHOME``
  cleared, this server's own venv removed — so console scripts and any
  nested ``python``/``pip`` the run script itself invokes resolve inside
  the venv too, exactly as after ``source .venv/bin/activate``.

Creation is guarded by a cross-process lock keyed on the project path:
one MCP server per agent is the normal way to run several agents against
one checkout, and they all start at once — without the lock, two of them
can both find no ``.venv`` and have uv recreate the directory under each
other's feet. The lock is held only for provisioning, never for a run.

Provisioning is skipped (an existing venv is still activated) when
``VUNIT_MCP_PYTHON`` names an interpreter explicitly, or when
``VUNIT_MCP_AUTO_VENV`` is falsy. It is a no-op — with an explanatory note
for ``vunit_status`` — when the project declares no dependencies or ``uv``
is not installed; the PATH interpreter is then used as before.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import os
import shutil
import subprocess
import tempfile
import time
from collections.abc import Iterator, Mapping, MutableMapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

#: Directory names searched for an existing project virtualenv, in order.
VENV_DIR_NAMES = (".venv", "venv")

#: Where a created virtualenv goes when the project has none.
CREATED_VENV_DIR_NAME = ".venv"

DEFAULT_VENV_TIMEOUT = 900.0

_FALSY = {"0", "false", "no", "off", "n"}


def venv_bin_dir_name() -> str:
    """The platform-specific scripts subdir name inside a virtualenv."""
    return "Scripts" if os.name == "nt" else "bin"


def _interpreter_names() -> tuple[str, ...]:
    return ("python.exe", "python3.exe") if os.name == "nt" else ("python3", "python")


def venv_interpreter(venv_dir: Path) -> Path | None:
    """The interpreter inside ``venv_dir``, or None if there is no venv there."""
    if not (venv_dir / "pyvenv.cfg").is_file():
        return None
    bin_dir = venv_dir / venv_bin_dir_name()
    for name in _interpreter_names():
        candidate = bin_dir / name
        if candidate.is_file():
            return candidate
    return None


def find_venv(project_dir: Path) -> Path | None:
    """An existing ``.venv``/``venv`` in ``project_dir`` (``.venv`` wins)."""
    for name in VENV_DIR_NAMES:
        candidate = project_dir / name
        if venv_interpreter(candidate) is not None:
            return candidate
    return None


def owning_venv(python: str | Path) -> Path | None:
    """The virtualenv ``python`` belongs to (``<venv>/bin/python`` -> venv).

    Used for an explicitly configured interpreter: if the user pointed at a
    venv's python, that venv is activated for the subprocess too instead of
    only being executed.
    """
    parent = Path(python).expanduser().parent
    for root in (parent.parent, parent):
        if (root / "pyvenv.cfg").is_file():
            return root
    return None


def auto_venv_enabled(env: Mapping[str, str], key: str) -> bool:
    """Whether ``env[key]`` permits creating a missing venv (default: yes)."""
    raw = env.get(key, "").strip().lower()
    return raw not in _FALSY if raw else True


@dataclass(frozen=True)
class VenvResult:
    """Outcome of resolving the project's virtualenv."""

    venv: Path | None
    notes: tuple[str, ...] = ()

    @property
    def interpreter(self) -> str | None:
        if self.venv is None:
            return None
        found = venv_interpreter(self.venv)
        return str(found) if found else None


@dataclass
class _Strategy:
    """One way to build the venv: a label plus the uv commands to run."""

    label: str
    argv_list: list[list[str]] = field(default_factory=list)


def _strategies(project_dir: Path, uv: str, venv_dir: Path) -> list[_Strategy]:
    """uv invocations to try, in order, until one yields a usable venv.

    ``uv sync`` is preferred for a ``pyproject.toml`` (it honors the
    project's lock file), but it only works for a pyproject that actually
    declares a ``[project]`` — plenty of HDL repos carry a pyproject solely
    for tool config and keep their real dependencies in ``requirements.txt``.
    Hence the ordered fallbacks rather than a single command.
    """
    pyproject = project_dir / "pyproject.toml"
    requirements = project_dir / "requirements.txt"
    create = [uv, "venv", str(venv_dir)]
    strategies: list[_Strategy] = []
    if pyproject.is_file():
        strategies.append(_Strategy("uv sync (pyproject.toml)", [[uv, "sync"]]))
    if requirements.is_file():
        strategies.append(
            _Strategy(
                "uv venv + uv pip install -r requirements.txt",
                [create, [uv, "pip", "install", "-r", str(requirements)]],
            )
        )
    if pyproject.is_file():
        strategies.append(
            _Strategy(
                "uv venv + uv pip install -r pyproject.toml",
                [create, [uv, "pip", "install", "-r", str(pyproject)]],
            )
        )
    return strategies


def _uv_env(venv_dir: Path, source: Mapping[str, str]) -> dict[str, str]:
    """Environment for the uv subprocesses.

    This server's own ``VIRTUAL_ENV`` must not leak: ``uv pip install``
    would otherwise install the *project's* dependencies into the *server's*
    virtualenv. ``UV_PROJECT_ENVIRONMENT`` is dropped for the same reason,
    and the target venv is named explicitly instead.
    """
    env = dict(source)
    own_venv = env.pop("VIRTUAL_ENV", None)
    env.pop("PYTHONHOME", None)
    env.pop("UV_PROJECT_ENVIRONMENT", None)
    if own_venv:
        own_bin = str(Path(own_venv) / venv_bin_dir_name())
        env["PATH"] = os.pathsep.join(
            entry for entry in env.get("PATH", "").split(os.pathsep) if entry != own_bin
        )
    env["VIRTUAL_ENV"] = str(venv_dir)
    env["UV_PROJECT_ENVIRONMENT"] = str(venv_dir)
    return env


def lock_path(project_dir: Path) -> Path:
    """Cross-process lock file for provisioning ``project_dir``'s venv.

    Kept in the temp dir rather than in the project: the lock is a
    machine-local runtime artifact, and the project directory may be a
    read-only or freshly cloned tree at this point.

    The name is deliberately server-neutral: an agent normally runs
    vunit-mcp *and* tsfpga-mcp against the same project, both of which
    provision the same ``.venv`` at startup. They must contend on the same
    lock file, so this name must stay byte-identical in both servers.
    """
    digest = hashlib.sha256(str(project_dir).encode("utf-8")).hexdigest()[:16]
    return Path(tempfile.gettempdir()) / f"hdl-mcp-project-venv-{digest}.lock"


@contextmanager
def _provisioning_lock(project_dir: Path, timeout: float) -> Iterator[bool]:
    """Hold the provisioning lock for ``project_dir``; yields whether it was taken.

    Uses ``O_CREAT|O_EXCL`` (atomic on every filesystem that matters here,
    unlike ``flock`` on NFS) with a deadline. Yielding False rather than
    raising on timeout keeps a stale lock file from wedging the server
    permanently -- the caller re-checks for a venv a peer may meanwhile
    have created, and otherwise proceeds unlocked.
    """
    path = lock_path(project_dir)
    deadline = time.monotonic() + timeout
    fd: int | None = None
    while True:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            break
        except FileExistsError:
            if time.monotonic() >= deadline:
                break
            time.sleep(0.2)
        except OSError as exc:  # unwritable temp dir: run unlocked
            if exc.errno in (errno.EACCES, errno.EPERM, errno.EROFS):
                break
            raise
    try:
        if fd is not None:
            os.write(fd, f"{os.getpid()}\n".encode())
        yield fd is not None
    finally:
        if fd is not None:
            os.close(fd)
            with contextlib.suppress(OSError):
                path.unlink()


def _first_error_line(proc: subprocess.CompletedProcess[str]) -> str:
    """The most informative single line of a failed uv invocation."""
    text = f"{proc.stderr}\n{proc.stdout}"
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for line in lines:
        if "error" in line.lower():
            return line
    return lines[-1] if lines else f"exit code {proc.returncode}"


def create_venv(
    project_dir: Path,
    *,
    uv: str,
    timeout: float,
    env: Mapping[str, str] | None = None,
    venv_dir_name: str = CREATED_VENV_DIR_NAME,
) -> VenvResult:
    """Create ``<project_dir>/.venv`` from the project's dependency spec.

    Returns the created venv, or ``VenvResult(None, notes)`` explaining why
    nothing was created. Never raises: a project without a venv still has
    the PATH-interpreter fallback, so a provisioning failure must degrade
    into a diagnosable note, not a dead server.
    """
    venv_dir = project_dir / venv_dir_name
    strategies = _strategies(project_dir, uv, venv_dir)
    if not strategies:
        return VenvResult(
            None,
            (
                f"no virtualenv in {project_dir} and no pyproject.toml/"
                "requirements.txt to create one from",
            ),
        )

    uv_env = _uv_env(venv_dir, os.environ if env is None else env)
    notes: list[str] = []
    for strategy in strategies:
        failure: str | None = None
        for argv in strategy.argv_list:
            try:
                proc = subprocess.run(
                    argv,
                    cwd=project_dir,
                    env=uv_env,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                failure = f"timed out after {timeout:.0f}s"
                break
            except OSError as exc:
                failure = str(exc)
                break
            if proc.returncode != 0:
                failure = _first_error_line(proc)
                break
        if failure is None and venv_interpreter(venv_dir) is not None:
            notes.append(f"created {venv_dir} via {strategy.label}")
            return VenvResult(venv_dir, tuple(notes))
        notes.append(
            f"{strategy.label} failed: {failure or 'no virtualenv was created'}"
        )
    return VenvResult(None, tuple(notes))


#: Appended when the venv could not be resolved under the provisioning
#: lock, so "there is a venv" could not be upgraded to "it is ready".
UNLOCKED_NOTE = (
    "could not take the provisioning lock at {lock} — if a peer was "
    "installing at the same time this virtualenv may be incomplete"
)


def ensure_venv(
    project_dir: Path,
    *,
    create: bool = True,
    uv: str | None = None,
    timeout: float = DEFAULT_VENV_TIMEOUT,
    env: Mapping[str, str] | None = None,
) -> VenvResult:
    """The project's virtualenv, creating one with uv if there is none.

    Discovery happens *inside* the provisioning lock, not before it. ``uv
    venv`` writes the interpreter before ``uv pip install`` writes a single
    package, so a virtualenv a peer is still provisioning looks perfectly
    finished to :func:`find_venv` for the whole install window — a peer
    that skipped the lock would hand back an empty environment and run.py
    would fail with ``No module named 'vunit'``. Taking the lock first is
    what makes "there is a virtualenv" mean "it is ready to use".

    The uncontended cost is one ``O_CREAT|O_EXCL`` file create plus an
    unlink, which is far too small to be worth a fast path that would
    reintroduce the race.
    """
    with _provisioning_lock(project_dir, timeout) as locked:
        unlocked_notes = (
            () if locked else (UNLOCKED_NOTE.format(lock=lock_path(project_dir)),)
        )
        # Either it was already there, or a peer finished it while we waited.
        existing = find_venv(project_dir)
        if existing is not None:
            return VenvResult(existing, unlocked_notes)
        if not create:
            return VenvResult(None, ("virtualenv auto-creation disabled",))
        uv_exe = uv or shutil.which("uv")
        if not uv_exe:
            return VenvResult(
                None,
                (
                    f"no virtualenv in {project_dir} and uv is not installed "
                    "— install uv (https://docs.astral.sh/uv/) or create "
                    "the virtualenv manually",
                ),
            )
        result = create_venv(project_dir, uv=uv_exe, timeout=timeout, env=env)
    return VenvResult(result.venv, (*result.notes, *unlocked_notes))


def activate(env: MutableMapping[str, str], venv: Path | None) -> None:
    """Make ``env`` look like a shell that activated ``venv``.

    Whatever virtualenv *this* process runs in is deactivated first (its
    ``VIRTUAL_ENV``/``PYTHONHOME`` describe the server, not the target),
    then ``venv`` — if any — is put in front of ``PATH``. Mutates ``env``.
    """
    own_venv = env.pop("VIRTUAL_ENV", None)
    env.pop("PYTHONHOME", None)
    entries = [entry for entry in env.get("PATH", "").split(os.pathsep) if entry]
    if own_venv:
        own_bin = str(Path(own_venv) / venv_bin_dir_name())
        entries = [entry for entry in entries if entry != own_bin]
    if venv is not None:
        bin_dir = str(venv / venv_bin_dir_name())
        entries = [bin_dir] + [entry for entry in entries if entry != bin_dir]
        env["VIRTUAL_ENV"] = str(venv)
    env["PATH"] = os.pathsep.join(entries)
