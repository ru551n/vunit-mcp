"""NVC ``--wave`` (upstream PR #1101) coverage against the pinned VUnit fork.

All tests require nvc on PATH and are skipped otherwise. They close the
gap the pure unit tests cannot close: vunit-mcp only decides *which* flags to
pass and *where* to look; these tests confirm the pinned VUnit fork's NVC
driver actually asks nvc for ``--wave=<file> --format=fst`` without
``--gui`` and that a real headless run produces the FST file vunit-mcp
hands off.

- driver level: NVCInterface.simulate() with wave=True, gui=False must
  build ``--wave=<out>/nvc/<entity>.fst`` + ``--format=fst`` (the command
  is captured, nvc is never executed);
- end to end: a real run of the fixture project records
  ``nvc/<entity>.fst`` headlessly and vunit_get_test_waveform resolves it
  (binary FST content, not VCD).
"""

import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from vunit.sim_if import nvc as nvc_module
from vunit.sim_if.nvc import NVCInterface
from vunit.vhdl_standard import VHDL

from vunit_mcp import server
from vunit_mcp.models import GetTestWaveformInput, RunTestsInput
from vunit_mcp.server import vunit_get_test_waveform, vunit_run_tests

NVC = shutil.which("nvc")
pytestmark = pytest.mark.skipif(NVC is None, reason="requires nvc on PATH")

FIXTURE_PROJECT = Path(__file__).parent / "fixture_project"


class _Library:
    name = "tb"
    directory = str(FIXTURE_PROJECT / "rtl")


class _Project:
    def get_libraries(self):
        return [_Library()]

    def get_library(self, name):
        return _Library()


def _make_sim(output_path, *, wave):
    """NVCInterface built the way VUnit builds it for run.py (from_args),
    minus the real argument parsing."""
    sim = NVCInterface(
        output_path=output_path,
        prefix=str(Path(NVC).parent),
        num_threads=1,
        gui=False,
        viewer_fmt="fst",
        wave=wave,
    )
    sim._project = _Project()
    sim._vhdl_standard = VHDL.STD_2008
    return sim


def _config_stub():
    return SimpleNamespace(
        library_name="tb",
        entity_name="tb_counter_fail",
        architecture_name="test",
        vhdl_configuration_name=None,
        generics={},
        vhdl_assert_stop_level="error",
        sim_options={},
    )


def test_nvc_driver_builds_headless_wave_fst_command(monkeypatch, tmp_path):
    captured = []

    class FakeProcess:
        def __init__(self, cmd):
            captured.append(list(cmd))

        def consume_output(self):
            pass

    monkeypatch.setattr(nvc_module, "Process", FakeProcess)
    output_path = str(tmp_path / "out")
    config = _config_stub()

    # --wave without --gui: the headless recording path.
    _make_sim(output_path, wave=True).simulate(
        output_path, "tb.tb_counter_fail.deliberately fails", config, elaborate_only=False
    )
    assert len(captured) == 1
    cmd = captured[0]
    wave_arg = next((a for a in cmd if a.startswith("--wave=")), None)
    assert wave_arg == f"--wave={tmp_path / 'out' / 'nvc' / 'tb_counter_fail.fst'}"
    assert "--format=fst" in cmd
    # nvc was never executed (Process faked), so the file must not exist
    # yet — recording is the simulator's job, driven by these flags.
    assert not (tmp_path / "out" / "nvc" / "tb_counter_fail.fst").exists()

    # Baseline: without --wave the driver passes the format hint but never
    # a --wave argument, so nvc records nothing (wave_file stays None).
    captured.clear()
    _make_sim(output_path, wave=False).simulate(
        output_path, "tb.tb_counter_fail.deliberately fails", config, elaborate_only=False
    )
    cmd = captured[0]
    assert not any(a.startswith("--wave=") for a in cmd)


@pytest.fixture
def fresh_server(monkeypatch):
    """A fresh server config per test (the server caches config/probes at
    first use in module globals)."""
    monkeypatch.setenv("VUNIT_MCP_PROJECT_DIR", str(FIXTURE_PROJECT))
    monkeypatch.setenv("VUNIT_MCP_SIMULATOR", "nvc")
    monkeypatch.setattr(server, "_config", None)
    monkeypatch.setattr(server, "_wave_flag_supported", None)
    monkeypatch.setattr(server, "_last_output_dir", None)
    yield


async def test_nvc_records_fst_headless_e2e(fresh_server, tmp_path):
    result = await vunit_run_tests(
        RunTestsInput(
            test_patterns=["tb.tb_counter_fail*"],
            waveform_format="fst",
            output_dir=str(tmp_path),
        )
    )
    # The fixture test fails by design; the run itself must succeed.
    assert result.startswith("Run FAILED.")
    assert "Waveforms recorded (FST)" in result

    waveform = await vunit_get_test_waveform(
        GetTestWaveformInput(test_name="tb.tb_counter_fail.deliberately fails")
    )
    path_line = next(line for line in waveform.splitlines() if line.startswith("Path: "))
    wave = Path(path_line.removeprefix("Path: ")).resolve()
    # NVC layout: <test_dir>/nvc/<entity>.fst (not GHDL's wave.<fmt>).
    assert wave.parent.name == "nvc"
    assert wave.name == "tb_counter_fail.fst"
    assert wave.is_file()

    raw = wave.read_bytes()
    assert len(raw) > 0
    # FST is binary (FST2 header); a VCD would start with ASCII $date.
    assert raw[:1] not in (b"$", b"#")

    assert "Format: FST" in waveform
    assert "Failing check at 50 ns" in waveform


async def test_nvc_normalizes_requested_format_to_fst(fresh_server, tmp_path):
    """The server records canonical fst on NVC even when another format is
    requested, and says so in the result."""
    result = await vunit_run_tests(
        RunTestsInput(
            test_patterns=["tb.tb_counter_fail*"],
            waveform_format="vcd",
            output_dir=str(tmp_path),
        )
    )
    assert "Waveform format normalized: recording fst" in result

    waveform = await vunit_get_test_waveform(
        GetTestWaveformInput(test_name="tb.tb_counter_fail.deliberately fails")
    )
    path_line = next(line for line in waveform.splitlines() if line.startswith("Path: "))
    wave = Path(path_line.removeprefix("Path: ")).resolve()
    # The recorded file is fst, not the requested vcd.
    assert wave.name == "tb_counter_fail.fst"
    assert wave.is_file()
