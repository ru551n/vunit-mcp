"""Tests for vunit_run_tests argv building and result handling, driven by a
fake run.py (no simulator needed).

The fake run.py emulates the VUnit CLI surface the server relies on: it
honors ``-x <path>`` by writing a JUnit XML, so the tests assert real
end-to-end tool behavior (report parsing, simulator-error gating, output
dir handling) without a simulator.
"""

import sys
from pathlib import Path

import pytest

from vunit_mcp import server
from vunit_mcp.config import Config
from vunit_mcp.models import RunTestsInput

FAKE_RUN_PY = """\
import sys
args = sys.argv[1:]
x = None
if "-x" in args:
    x = args[args.index("-x") + 1]
if "NO_JUNIT" in sys.argv:
    print("VUnit: No available simulator detected.")
    sys.exit(1)
if "NO_JUNIT_QUIET" in sys.argv:
    print("VUnit: compilation failed (no report written).")
    sys.exit(1)
# Every mode that produces a JUnit also echoes the "no simulator" marker
# in (test) output: a run that completes must be judged by its report,
# not by this line.
print("test output: No available simulator detected. (DUT log line)")
if x is not None:
    failed = "SIM_OUT_PASS" not in sys.argv
    body = '<failure message="deliberate failure"/>' if failed else ""
    with open(x, "w") as f:
        f.write(
            '<testsuite name="vunit" tests="1" failures="'
            + ("1" if failed else "0")
            + '" errors="0" skipped="0" time="1.0">'
            '<testcase classname="tb.t_a" name="tb.t_a.test1" time="1.0">'
            + body
            + "</testcase></testsuite>"
        )
print("ok")
"""


def _config(project: Path, extra_args: list[str] | None = None) -> Config:
    return Config(
        project_dir=project,
        run_script=project / "run.py",
        python=sys.executable,
        simulator=None,
        output_dir=project / "vunit_out",
        timeout=30.0,
        extra_args=extra_args if extra_args is not None else [],
        fingerprint_exclude=[],
    )


def _run_with_env(project: Path, extra_args: tuple[str, ...] = (), **kwargs) -> str:
    """vunit_run_tests against `project` (module state reset by the
    fresh_server fixture)."""
    import asyncio

    server._config = _config(project, list(extra_args))
    return asyncio.run(server.vunit_run_tests(RunTestsInput(**kwargs)))


@pytest.fixture
def fake_project(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    (project / "run.py").write_text(FAKE_RUN_PY, encoding="utf-8")
    return project


@pytest.fixture
def fresh_server(fake_project, monkeypatch):
    """Reset the server's module-global state and point the config env at
    the fake project."""
    monkeypatch.setenv("VUNIT_MCP_PROJECT_DIR", str(fake_project))
    monkeypatch.setenv("VUNIT_MCP_SIMULATOR", "")
    monkeypatch.delenv("VUNIT_SIMULATOR", raising=False)
    server._config = None
    server._wave_flag_supported = None
    server._last_output_dir = None
    yield fake_project


# --- _run_args ---------------------------------------------------------------


def test_run_args_with_without_attributes(fake_project):
    """VUnit requires the attribute name as the flag's value
    (argparse action="append") — a bare flag is an argparse error."""
    args = server._run_args(
        RunTestsInput(
            test_patterns=["tb.*"],
            with_attributes=["regression"],
            without_attributes=["slow", "nightly"],
        ),
        fake_project / "out",
    )
    assert args.index("--with-attributes") < args.index("regression")
    assert args[args.index("--with-attributes") + 1] == "regression"
    assert args[args.index("--without-attributes") + 1] == "slow"
    assert args.index("--without-attributes", args.index("slow")) + 1 == args.index(
        "nightly"
    )
    # Repeated flag per name, both names present.
    assert args.count("--with-attributes") == 1
    assert args.count("--without-attributes") == 2
    assert args[-1] == "tb.*"


def test_run_args_no_attribute_flags_when_empty(fake_project):
    args = server._run_args(RunTestsInput(test_patterns=["tb.*"]), fake_project / "out")
    assert "--with-attributes" not in args
    assert "--without-attributes" not in args


# --- simulator-error gating --------------------------------------------------


def test_run_tests_reports_failure_without_false_simulator_error(fresh_server):
    """A failing run whose output contains the 'no simulator' marker must be
    reported from the JUnit, not as a missing simulator."""
    out = _run_with_env(fresh_server)
    assert out.startswith("Run FAILED.")
    assert "No simulator available" not in out
    assert "tb.t_a.test1" in out


def test_successful_run_ignores_simulator_marker_in_test_output(fresh_server):
    """A green run whose test output contains the marker is reported from
    the JUnit."""
    out = _run_with_env(fresh_server, extra_args=("SIM_OUT_PASS",))
    assert out.startswith("Run PASSED.")
    assert "No simulator available" not in out


def test_early_failure_without_junit_reports_simulator_error(fresh_server):
    """A run that dies before producing a JUnit: the marker is the
    actionable error."""
    out = _run_with_env(fresh_server, extra_args=("NO_JUNIT",))
    assert "No simulator available to VUnit" in out
    assert "No available simulator detected." in out
    assert "Install a simulator or set VUNIT_MCP_SIMULATOR" in out


# --- run timeout --------------------------------------------------------------


def test_run_vunit_times_out_and_reports(fresh_server, tmp_path):
    """A hung run.py is killed (whole process group) and the run reports
    the timeout instead of hanging the server."""
    hung = tmp_path / "hung"
    hung.mkdir()
    (hung / "run.py").write_text("import time\ntime.sleep(5)\n", encoding="utf-8")
    out = _run_with_env(hung, timeout=0.5)
    assert "Timed out after" in out


# --- output dir pointer -------------------------------------------------------


def test_failed_run_does_not_move_report_pointer(fresh_server):
    """A run that produces no fresh JUnit must not point get_report at its
    (empty) output dir: the last completed run's results stay reachable."""
    out = _run_with_env(fresh_server)
    assert out.startswith("Run FAILED.")
    assert server._last_output_dir == fresh_server / "vunit_out"

    # A failing run in an override dir leaves the pointer on the good dir.
    out = _run_with_env(
        fresh_server,
        extra_args=("NO_JUNIT_QUIET",),
        output_dir=str(fresh_server / "alt_out"),
    )
    assert "no JUnit file found" in out
    assert server._last_output_dir == fresh_server / "vunit_out"
