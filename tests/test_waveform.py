"""Tests for waveform.py (time parsing, anchor extraction, file resolution).

Uses the test log recorded from the fixture's failing test
(tests/data/fixture_wave/output.txt) plus temporary output trees mimicking
VUnit's layout. No simulator required.
"""

from decimal import Decimal
from pathlib import Path

import pytest

from vunit_mcp.waveform import (
    find_anchor_from_log,
    find_waveform_file,
    format_seconds,
    help_supports_wave_flag,
    parse_time_str,
    run_waveform_args,
    waveform_unavailable_reason,
)

DATA = Path(__file__).parent / "data" / "fixture_wave"
LOG_PATH = DATA / "output.txt"


# --- parse_time_str ----------------------------------------------------------


def test_parse_time_str_units():
    assert parse_time_str("50 ns") == Decimal("5e-8")
    assert parse_time_str("50000000 fs") == Decimal("5e-8")
    assert parse_time_str("1us") == Decimal("1e-6")
    assert parse_time_str("1.5 ms") == Decimal("1.5e-3")
    assert parse_time_str("2 s") == Decimal(2)
    assert parse_time_str("1 µs") == Decimal("1e-6")


@pytest.mark.parametrize("bad", ["", "50", "ns", "50 xy", "5 ns 3 ns", "abc ns"])
def test_parse_time_str_invalid(bad):
    assert parse_time_str(bad) is None


# --- format_seconds ----------------------------------------------------------


@pytest.mark.parametrize(
    ("secs", "expected"),
    [
        (Decimal("5e-8"), "50 ns"),
        (Decimal("1.5e-12"), "1.5 ps"),
        (Decimal("1e-6"), "1 us"),
        (Decimal("1e-3"), "1 ms"),
        (Decimal(2), "2 s"),
        (Decimal(0), "0 s"),
    ],
)
def test_format_seconds(secs, expected):
    assert format_seconds(secs) == expected


# --- anchor extraction from the test log --------------------------------------


def test_find_anchor_from_log_real():
    secs, msg = find_anchor_from_log(LOG_PATH.read_text(encoding="utf-8"))
    assert secs == Decimal("5e-8")
    assert msg.startswith("expected 99")


def test_find_anchor_from_log_none():
    assert find_anchor_from_log("all fine\nno errors here\n") == (None, "")


# --- find_waveform_file --------------------------------------------------------


def _recorded(tmp_path: Path, *names: str) -> None:
    ghdl = tmp_path / "ghdl"
    ghdl.mkdir()
    for name in names:
        (ghdl / name).write_text("", encoding="utf-8")


def test_find_waveform_file_vcd_preferred(tmp_path):
    _recorded(tmp_path, "wave.vcd", "wave.ghw")
    assert find_waveform_file(tmp_path) == tmp_path / "ghdl" / "wave.vcd"


def test_find_waveform_file_nvc_entity_name(tmp_path):
    nvc = tmp_path / "nvc"
    nvc.mkdir()
    (nvc / "tb_counter.vcd").write_text("$end\n", encoding="utf-8")
    assert find_waveform_file(tmp_path) == tmp_path / "nvc" / "tb_counter.vcd"


def test_find_waveform_file_nvc_fst(tmp_path):
    # FST is NVC's default machine-readable format with --wave.
    nvc = tmp_path / "nvc"
    nvc.mkdir()
    (nvc / "tb_counter.fst").write_bytes(b"\x00")
    assert find_waveform_file(tmp_path) == tmp_path / "nvc" / "tb_counter.fst"
    assert find_waveform_file(tmp_path, "fst") == tmp_path / "nvc" / "tb_counter.fst"
    assert find_waveform_file(tmp_path, "vcd") is None


def test_find_waveform_file_nvc_vcd_preferred_over_fst(tmp_path):
    nvc = tmp_path / "nvc"
    nvc.mkdir()
    (nvc / "tb_counter.vcd").write_text("$end\n", encoding="utf-8")
    (nvc / "tb_counter.fst").write_bytes(b"\x00")
    assert find_waveform_file(tmp_path) == tmp_path / "nvc" / "tb_counter.vcd"


def test_find_waveform_file_wave_name_preferred(tmp_path):
    _recorded(tmp_path, "wave.vcd")
    nvc = tmp_path / "nvc"
    nvc.mkdir()
    (nvc / "tb_counter.vcd").write_text("$end\n", encoding="utf-8")
    assert find_waveform_file(tmp_path) == tmp_path / "ghdl" / "wave.vcd"


def test_find_waveform_file_ghw_only(tmp_path):
    _recorded(tmp_path, "wave.ghw")
    assert find_waveform_file(tmp_path) == tmp_path / "ghdl" / "wave.ghw"


def test_find_waveform_file_explicit_fmt(tmp_path):
    _recorded(tmp_path, "wave.vcd", "wave.ghw")
    assert find_waveform_file(tmp_path, "ghw") == tmp_path / "ghdl" / "wave.ghw"
    assert find_waveform_file(tmp_path, "vcd") == tmp_path / "ghdl" / "wave.vcd"


def test_find_waveform_file_fst_preferred_over_ghw(tmp_path):
    _recorded(tmp_path, "wave.fst", "wave.ghw")
    assert find_waveform_file(tmp_path) == tmp_path / "ghdl" / "wave.fst"


def test_find_waveform_file_missing_format(tmp_path):
    _recorded(tmp_path, "wave.ghw")
    assert find_waveform_file(tmp_path, "vcd") is None


def test_find_waveform_file_none_recorded(tmp_path):
    (tmp_path / "ghdl").mkdir()
    assert find_waveform_file(tmp_path) is None
    assert find_waveform_file(Path("/nonexistent")) is None


# --- help_supports_wave_flag -------------------------------------------------


def test_help_supports_wave_flag_positive():
    help_text = (
        "usage: run.py [-h] [--wave] [--wave-fmt {vcd,fst,ghw}]\n"
        "  --wave  Generate waveforms.\n"
    )
    assert help_supports_wave_flag(help_text)


def test_help_supports_wave_flag_negative():
    # Legacy VUnit: only --gtkwave-fmt / --viewer-fmt, no --wave.
    help_text = (
        "usage: run.py [-h] [--gtkwave-fmt {vcd,fst,ghw}]\n"
        "  --gtkwave-fmt FMT  Select the format for waveforms.\n"
    )
    assert not help_supports_wave_flag(help_text)


def test_help_supports_wave_flag_empty():
    assert not help_supports_wave_flag("")


def test_help_supports_wave_flag_waves_spelling():
    # PR title uses --waves; a future release might keep that spelling.
    assert help_supports_wave_flag("  --waves  Generate waveforms.\n")


# --- run_waveform_args --------------------------------------------------------


def test_run_waveform_args_new_flag():
    for fmt in ("vcd", "ghw", "fst"):
        assert run_waveform_args(fmt, True) == ["--wave", "--wave-fmt", fmt]


def test_run_waveform_args_legacy_vcd_ghw():
    assert run_waveform_args("vcd", False) == ["--gtkwave-fmt", "vcd"]
    assert run_waveform_args("ghw", False) == ["--gtkwave-fmt", "ghw"]


def test_run_waveform_args_legacy_fst_raises():
    with pytest.raises(ValueError, match="--wave"):
        run_waveform_args("fst", False)


# --- waveform_unavailable_reason --------------------------------------------


def test_wave_available_new_flag_all_sims():
    for sim in ("ghdl", "nvc", "vsim", None):
        assert waveform_unavailable_reason(sim, True) is None


def test_wave_unavailable_nvc_legacy():
    for sim in ("nvc", "NVC", " nvc "):
        reason = waveform_unavailable_reason(sim, False)
        assert reason is not None
        assert "NVC" in reason


def test_wave_available_ghdl_and_unknown_legacy():
    # Legacy VUnit: GHDL records headlessly; an unknown/auto-detected
    # simulator is assumed to take the GHDL path (no false alarm).
    assert waveform_unavailable_reason("ghdl", False) is None
    assert waveform_unavailable_reason(None, False) is None
    assert waveform_unavailable_reason("vsim", False) is None
