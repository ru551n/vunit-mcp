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
if x is not None:
    with open(x, "w") as f:
        f.write(
            '<testsuite name="vunit" tests="1" failures="1" errors="0" skipped="0" time="1.0">'
            '<testcase classname="tb.t_a" name="tb.t_a.test1" time="1.0">'
            '<failure message="deliberate failure"/>test output '
            'No available simulator detected. from the DUT log line</failure>'
            '</testcase></testsuite>'
        )
print("ok")
"""


def _config(project: Path) -> Config:
    return Config(
        project_dir=project,
        run_script=project / "run.py",
        python=sys.executable,
        simulator=None,
        output_dir=project / "vunit_out",
        timeout=30.0,
        extra_args=[],
        fingerprint_exclude=[],
    )


def _run_with_env(project: Path, **kwargs) -> str:
    import asyncio

    server._config = _config(project)
    server._wave_flag_supported = None
    server._last_output_dir = None
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
    args = server._run_args(
        RunTestsInput(test_patterns=["tb.*"]), fake_project / "out"
    )
    assert "--with-attributes" not in args
    assert "--without-attributes" not in args