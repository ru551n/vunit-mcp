"""Tests for project virtualenv discovery/creation/activation."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from vunit_mcp.project_venv import (
    _provisioning_lock,
    activate,
    auto_venv_enabled,
    create_venv,
    ensure_venv,
    find_venv,
    owning_venv,
    venv_bin_dir_name,
    venv_interpreter,
)

BIN = venv_bin_dir_name()


def _fake_venv(root: Path) -> Path:
    """A directory that looks like a virtualenv to every helper here."""
    (root / BIN).mkdir(parents=True)
    (root / "pyvenv.cfg").write_text("home = /usr\n", encoding="utf-8")
    exe = root / BIN / ("python.exe" if os.name == "nt" else "python3")
    exe.write_text("", encoding="utf-8")
    exe.chmod(0o755)
    return root


# --- discovery ---------------------------------------------------------------


def test_find_venv_prefers_dot_venv(tmp_path):
    _fake_venv(tmp_path / ".venv")
    _fake_venv(tmp_path / "venv")
    assert find_venv(tmp_path) == tmp_path / ".venv"


def test_find_venv_accepts_plain_venv(tmp_path):
    _fake_venv(tmp_path / "venv")
    assert find_venv(tmp_path) == tmp_path / "venv"


def test_directory_without_pyvenv_cfg_is_not_a_venv(tmp_path):
    """A stray '.venv' dir (e.g. an empty leftover) must not be adopted."""
    (tmp_path / ".venv" / BIN).mkdir(parents=True)
    (tmp_path / ".venv" / BIN / "python3").write_text("", encoding="utf-8")
    assert venv_interpreter(tmp_path / ".venv") is None
    assert find_venv(tmp_path) is None


def test_owning_venv_of_interpreter(tmp_path):
    venv = _fake_venv(tmp_path / ".venv")
    assert owning_venv(venv / BIN / "python3") == venv
    assert owning_venv("/usr/bin/python3") is None


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "OFF"])
def test_auto_venv_disabled(value):
    assert not auto_venv_enabled({"K": value}, "K")


@pytest.mark.parametrize("value", ["", "1", "true", "yes"])
def test_auto_venv_enabled_by_default(value):
    assert auto_venv_enabled({"K": value}, "K")
    assert auto_venv_enabled({}, "K")


# --- activation --------------------------------------------------------------


def test_activate_puts_venv_first_on_path(tmp_path):
    venv = _fake_venv(tmp_path / ".venv")
    env = {"PATH": os.pathsep.join(["/usr/bin", "/bin"])}
    activate(env, venv)
    assert env["VIRTUAL_ENV"] == str(venv)
    assert env["PATH"].split(os.pathsep)[0] == str(venv / BIN)
    assert "/usr/bin" in env["PATH"].split(os.pathsep)


def test_activate_deactivates_this_servers_own_venv(tmp_path):
    own = _fake_venv(tmp_path / "server_venv")
    target = _fake_venv(tmp_path / "proj" / ".venv")
    env = {
        "VIRTUAL_ENV": str(own),
        "PYTHONHOME": "/should/be/removed",
        "PATH": os.pathsep.join([str(own / BIN), "/usr/bin"]),
    }
    activate(env, target)
    assert env["VIRTUAL_ENV"] == str(target)
    assert "PYTHONHOME" not in env
    entries = env["PATH"].split(os.pathsep)
    assert str(own / BIN) not in entries
    assert entries[0] == str(target / BIN)


def test_activate_without_target_only_deactivates(tmp_path):
    own = _fake_venv(tmp_path / "server_venv")
    env = {"VIRTUAL_ENV": str(own), "PATH": os.pathsep.join([str(own / BIN), "/bin"])}
    activate(env, None)
    assert "VIRTUAL_ENV" not in env
    assert env["PATH"] == "/bin"


def test_activate_does_not_duplicate_bin_dir(tmp_path):
    venv = _fake_venv(tmp_path / ".venv")
    env = {"PATH": os.pathsep.join([str(venv / BIN), "/usr/bin"])}
    activate(env, venv)
    assert env["PATH"].split(os.pathsep).count(str(venv / BIN)) == 1


# --- provisioning ------------------------------------------------------------


def test_existing_venv_is_reused_and_never_recreated(tmp_path):
    venv = _fake_venv(tmp_path / ".venv")
    result = ensure_venv(tmp_path, uv="/nonexistent/uv")
    assert result.venv == venv
    assert result.notes == ()


def test_no_spec_no_creation(tmp_path):
    result = ensure_venv(tmp_path, uv="/nonexistent/uv")
    assert result.venv is None
    assert "no pyproject.toml/requirements.txt" in result.notes[0]


def test_creation_can_be_disabled(tmp_path):
    (tmp_path / "requirements.txt").write_text("", encoding="utf-8")
    result = ensure_venv(tmp_path, create=False)
    assert result.venv is None
    assert result.notes == ("virtualenv auto-creation disabled",)


def test_missing_uv_is_reported_not_raised(tmp_path, monkeypatch):
    (tmp_path / "requirements.txt").write_text("", encoding="utf-8")
    monkeypatch.setattr(shutil, "which", lambda *_a, **_kw: None)
    result = ensure_venv(tmp_path)
    assert result.venv is None
    assert "uv is not installed" in result.notes[0]


def test_failing_uv_falls_back_to_the_next_strategy(tmp_path):
    """A pyproject uv sync cannot handle must not shadow requirements.txt."""
    (tmp_path / "pyproject.toml").write_text("[tool.black]\n", encoding="utf-8")
    (tmp_path / "requirements.txt").write_text("", encoding="utf-8")
    fake_uv = tmp_path / "fake_uv"
    fake_uv.write_text(
        textwrap.dedent(
            f"""\
            #!{sys.executable}
            import pathlib, sys
            if sys.argv[1] == "sync":
                print("error: no `project` table found", file=sys.stderr)
                sys.exit(2)
            if sys.argv[1] == "venv":
                root = pathlib.Path(sys.argv[2])
                (root / "{BIN}").mkdir(parents=True, exist_ok=True)
                (root / "pyvenv.cfg").write_text("home = /usr\\n")
                (root / "{BIN}" / "python3").write_text("")
            """
        ),
        encoding="utf-8",
    )
    fake_uv.chmod(0o755)

    result = create_venv(tmp_path, uv=str(fake_uv), timeout=30.0)

    assert result.venv == tmp_path / ".venv"
    assert (
        "uv sync (pyproject.toml) failed: error: no `project` table" in result.notes[0]
    )
    assert "created" in result.notes[-1]
    assert "requirements.txt" in result.notes[-1]


def test_all_strategies_failing_is_reported_not_raised(tmp_path):
    (tmp_path / "requirements.txt").write_text("", encoding="utf-8")
    result = create_venv(tmp_path, uv="/nonexistent/uv", timeout=5.0)
    assert result.venv is None
    assert result.notes and "failed" in result.notes[0]


# --- real uv (integration) ---------------------------------------------------

uv_required = pytest.mark.skipif(
    shutil.which("uv") is None, reason="uv is not installed"
)


@uv_required
def test_real_uv_creates_venv_from_requirements(tmp_path):
    (tmp_path / "requirements.txt").write_text("", encoding="utf-8")
    result = ensure_venv(tmp_path, timeout=300.0)
    assert result.venv == tmp_path / ".venv"
    interpreter = result.interpreter
    assert interpreter is not None
    probe = subprocess.run(
        [interpreter, "-c", "import sys; print(sys.prefix)"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert probe.stdout.strip() == str(tmp_path / ".venv")


@uv_required
def test_real_uv_creates_venv_from_pyproject(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        textwrap.dedent(
            """\
            [project]
            name = "demo"
            version = "0.1.0"
            requires-python = ">=3.10"
            dependencies = []

            [tool.uv]
            package = false
            """
        ),
        encoding="utf-8",
    )
    result = ensure_venv(tmp_path, timeout=300.0)
    assert result.venv == tmp_path / ".venv"
    assert result.interpreter is not None
    assert "uv sync" in result.notes[0]


@uv_required
def test_real_uv_does_not_install_into_this_servers_venv(tmp_path, monkeypatch):
    """VIRTUAL_ENV of the server must not capture the project's install."""
    own = _fake_venv(tmp_path / "server_venv")
    monkeypatch.setenv("VIRTUAL_ENV", str(own))
    monkeypatch.setenv("UV_PROJECT_ENVIRONMENT", str(own))
    project = tmp_path / "proj"
    project.mkdir()
    (project / "requirements.txt").write_text("", encoding="utf-8")

    result = ensure_venv(project, timeout=300.0)

    assert result.venv == project / ".venv"
    assert not (own / "lib").exists()


# --- concurrent agents -------------------------------------------------------


def test_provisioning_lock_is_per_project(tmp_path):
    """Separate worktrees must never contend: the lock is keyed on the path."""
    from vunit_mcp.project_venv import lock_path

    assert lock_path(tmp_path / "wt-a") != lock_path(tmp_path / "wt-b")
    assert lock_path(tmp_path / "wt-a") == lock_path(tmp_path / "wt-a")


def test_held_lock_does_not_wedge_provisioning(tmp_path, monkeypatch):
    """A stale lock (crashed peer) degrades to an unlocked, noted run rather
    than blocking the server forever."""
    from vunit_mcp.project_venv import lock_path

    (tmp_path / "requirements.txt").write_text("", encoding="utf-8")
    lock = lock_path(tmp_path)
    lock.write_text("99999\n", encoding="utf-8")
    monkeypatch.setattr(shutil, "which", lambda *_a, **_kw: "/nonexistent/uv")
    try:
        result = ensure_venv(tmp_path, timeout=0.5)
    finally:
        lock.unlink()
    assert result.venv is None
    assert any("provisioning lock" in note for note in result.notes)


def test_discovery_waits_for_a_peer_that_is_still_installing(tmp_path):
    """The regression that made discovery move inside the lock.

    ``uv venv`` writes the interpreter before ``uv pip install`` writes a
    single package, so for the whole install window a peer's half-built
    virtualenv is indistinguishable from a finished one. Checking before
    taking the lock therefore handed back an environment with no VUnit in
    it, and run.py died with ``No module named 'vunit'``.
    """
    (tmp_path / "requirements.txt").write_text("", encoding="utf-8")
    finished = threading.Event()
    took_lock = threading.Event()

    def peer() -> None:
        with _provisioning_lock(tmp_path, 10.0) as locked:
            assert locked, "the peer must win the lock for this test to mean anything"
            # `uv venv` has run: the interpreter is there, packages are not.
            _fake_venv(tmp_path / ".venv")
            took_lock.set()
            time.sleep(0.3)  # `uv pip install` still running
            finished.set()

    thread = threading.Thread(target=peer)
    thread.start()
    try:
        assert took_lock.wait(5.0)
        result = ensure_venv(tmp_path, uv="/nonexistent/uv", timeout=10.0)
        # Sampled here, not after the join below, which would wait for the
        # peer itself and make the assertion vacuous.
        peer_had_finished = finished.is_set()
    finally:
        thread.join()

    assert peer_had_finished, (
        "ensure_venv returned a virtualenv the peer had not finished installing"
    )
    assert result.venv == tmp_path / ".venv"


def test_peer_created_venv_is_adopted(tmp_path, monkeypatch):
    """Losing the race is not an error: the peer's venv is used as-is."""
    (tmp_path / "requirements.txt").write_text("", encoding="utf-8")
    venv = _fake_venv(tmp_path / ".venv")

    calls: list[list[str]] = []

    def _explode(argv, **_kw):
        calls.append(argv)
        raise AssertionError("must not run uv when a venv already exists")

    monkeypatch.setattr(subprocess, "run", _explode)
    result = ensure_venv(tmp_path, uv="/nonexistent/uv")
    assert result.venv == venv
    assert calls == []
