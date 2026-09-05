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
from vunit_mcp.models import GetTestLogInput, GetTestWaveformInput, RunTestsInput

FAKE_RUN_PY = """\
import os
import sys
args = sys.argv[1:]
x = None
if "-x" in args:
    x = args[args.index("-x") + 1]
if os.environ.get("FAKE_COMPILE_FAIL") == "1" and "--compile" in args:
    # Head-placed analyzer error + >4000 chars of filler: the tail-keeping
    # summary() would lose the error, error_excerpt(full_text) must not.
    print("HEAD_ERROR ghdl:error: at tb_counter.vhd:56: process has no wait statement")
    for _ in range(1500):
        print("filler filler")
    print("ghdl:error: compilation failed")
    sys.exit(1)
if "NO_JUNIT" in sys.argv:
    print("VUnit: No available simulator detected.")
    sys.exit(1)
if "NO_JUNIT_QUIET" in sys.argv:
    print("VUnit: compilation failed (no report written).")
    sys.exit(1)
if "EMPTY_JUNIT" in sys.argv:
    # Emulates a pattern that matched nothing: run.py succeeds and writes
    # a JUnit report with zero test cases.
    if x is not None:
        with open(x, "w") as f:
            f.write(
                '<testsuite name="vunit" tests="0" failures="0" errors="0" '
                'skipped="0" time="0.0"></testsuite>'
            )
    print("ok")
    sys.exit(0)
# Every mode that produces a JUnit also echoes the "no simulator" marker
# in (test) output: a run that completes must be judged by its report,
# not by this line.
print("test output: No available simulator detected. (DUT log line)")
sim = os.environ.get("VUNIT_SIMULATOR", "<unset>")
print("VUNIT_SIMULATOR=" + sim)
# Also record it (cwd is the project dir) for tests that assert on the
# subprocess environment.
with open(os.path.join(os.getcwd(), "sim_env.txt"), "w") as f:
    f.write(sim)
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


# --- per-call simulator override ------------------------------------------------


def test_run_tests_per_call_simulator(fresh_server):
    """A per-call simulator reaches the run.py subprocess as
    VUNIT_SIMULATOR (server-level setting is empty here)."""
    _run_with_env(fresh_server, simulator="nvc")
    assert (fresh_server / "sim_env.txt").read_text() == "nvc"


def test_compile_per_call_simulator(fresh_server):
    import asyncio

    out = asyncio.run(server.vunit_compile(simulator="ghdl"))
    assert out.startswith("Compile succeeded.")
    assert (fresh_server / "sim_env.txt").read_text() == "ghdl"


def test_per_call_simulator_beats_server_level(fake_project, monkeypatch):
    """Precedence: per-call > VUNIT_MCP_SIMULATOR (config) > VUNIT_SIMULATOR."""
    import asyncio

    monkeypatch.delenv("VUNIT_SIMULATOR", raising=False)
    server._config = Config(
        project_dir=fake_project,
        run_script=fake_project / "run.py",
        python=sys.executable,
        simulator="ghdl",
        output_dir=fake_project / "vunit_out",
        timeout=30.0,
        extra_args=[],
        fingerprint_exclude=[],
    )
    asyncio.run(server.vunit_run_tests(RunTestsInput(simulator="nvc")))
    assert (fake_project / "sim_env.txt").read_text() == "nvc"
    server._config = None


# --- compile failure output ------------------------------------------------------


def test_compile_failure_keeps_head_errors(fresh_server, monkeypatch):
    """Analyzer errors sit at the HEAD of the output; they must be surfaced
    even when the output far exceeds the summary's tail window."""
    import asyncio

    monkeypatch.setenv("FAKE_COMPILE_FAIL", "1")
    out = asyncio.run(server.vunit_compile())
    assert out.startswith("Error: Compile failed:")
    assert "HEAD_ERROR ghdl:error: at tb_counter.vhd:56" in out
    assert "ghdl:error: compilation failed" in out
    # Bounded: the ~9 KB filler body is not echoed in full.
    assert len(out) < 2000


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


# --- failure-shaped strings all start with "Error: " --------------------------
#
# Every tool failure path returns a plain string (no exception, no MCP
# isError) — the one thing tying them together is a consistent "Error: "
# prefix an agent can reliably match on. A couple of previously
# inconsistent paths (unprefixed "No ..."/"empty output" strings) are
# checked explicitly here; see server._err for the convention.


def test_no_tests_run_message_has_error_prefix(fresh_server):
    """A run whose patterns match nothing used to say "No tests were run
    ..." with no error marker; it must now be prefixed."""
    out = _run_with_env(fresh_server, extra_args=("EMPTY_JUNIT",))
    assert out.startswith("Error: ")
    assert "no tests were run" in out.lower()


def test_get_test_log_unknown_test_has_error_prefix(fresh_server):
    """ "No log found for test ..." used to have no error marker."""
    import asyncio

    server._config = _config(fresh_server)
    out = asyncio.run(server.vunit_get_test_log(GetTestLogInput(test_name="nope")))
    assert out.startswith("Error: ")
    assert "No log found for test" in out


def test_get_test_waveform_unknown_test_has_error_prefix(fresh_server):
    """ "No data for test ..." used to have no error marker."""
    import asyncio

    server._config = _config(fresh_server)
    out = asyncio.run(
        server.vunit_get_test_waveform(GetTestWaveformInput(test_name="nope"))
    )
    assert out.startswith("Error: ")
    assert "No data for test" in out


def test_get_report_missing_junit_has_error_prefix(fresh_server):
    """ "No JUnit report found ..." used to have no error marker."""
    import asyncio

    server._config = _config(fresh_server)
    out = asyncio.run(server.vunit_get_report())
    assert out.startswith("Error: ")
    assert "No JUnit report found" in out


def test_config_error_has_error_prefix(monkeypatch):
    """A ConfigError-driven failure (e.g. bad project dir) must also start
    with "Error: ", not the old bare "Configuration error: ..."."""
    import asyncio

    monkeypatch.setenv("VUNIT_MCP_PROJECT_DIR", "/does/not/exist")
    server._config = None
    try:
        out = asyncio.run(server.vunit_list_tests())
    finally:
        server._config = None
    assert out.startswith("Error: ")
