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
    parse_time_str,
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


def test_find_waveform_file_ghw_only(tmp_path):
    _recorded(tmp_path, "wave.ghw")
    assert find_waveform_file(tmp_path) == tmp_path / "ghdl" / "wave.ghw"


def test_find_waveform_file_explicit_fmt(tmp_path):
    _recorded(tmp_path, "wave.vcd", "wave.ghw")
    assert find_waveform_file(tmp_path, "ghw") == tmp_path / "ghdl" / "wave.ghw"
    assert find_waveform_file(tmp_path, "vcd") == tmp_path / "ghdl" / "wave.vcd"


def test_find_waveform_file_missing_format(tmp_path):
    _recorded(tmp_path, "wave.ghw")
    assert find_waveform_file(tmp_path, "vcd") is None


def test_find_waveform_file_none_recorded(tmp_path):
    (tmp_path / "ghdl").mkdir()
    assert find_waveform_file(tmp_path) is None
    assert find_waveform_file(Path("/nonexistent")) is None
