"""Tests for waveform.py (VCD time conversion, lookup, rendering).

Uses the real GHDL VCD recorded from the fixture's failing test
(tests/data/fixture_wave: wave.vcd + output.txt) plus a small hand-rolled
VCD string. No simulator required.
"""

import os
from decimal import Decimal
from pathlib import Path

import pytest
import vcdvcd

from vunit_mcp.waveform import (
    WaveformError,
    clear_vcd_cache,
    find_anchor_from_log,
    format_ticks,
    get_vcd,
    parse_time_str,
    render_waveform,
    resolve_signals,
    seconds_to_ticks,
    signal_names,
)

DATA = Path(__file__).parent / "data" / "fixture_wave"
VCD_PATH = DATA / "wave.vcd"
LOG_PATH = DATA / "output.txt"

FIXED_TEST = "tb.tb_counter_fail.deliberately fails"


@pytest.fixture(autouse=True)
def _fresh_cache():
    clear_vcd_cache()
    yield


def _fixture_vcd() -> vcdvcd.VCDVCD:
    return get_vcd(VCD_PATH)


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


# --- tick conversion (fixture VCD timescale: 1 fs) ---------------------------


def test_format_ticks_fixture():
    v = _fixture_vcd()
    assert format_ticks(0, v) == "0 fs"
    assert format_ticks(50000000, v) == "50 ns"
    assert format_ticks(25000000, v) == "25 ns"
    assert format_ticks(1500, v) == "1.5 ps"


def test_seconds_to_ticks_fixture():
    v = _fixture_vcd()
    assert seconds_to_ticks(Decimal("5e-8"), v) == 50000000
    assert seconds_to_ticks(Decimal(0), v) == 0


def test_vcd_without_timescale_raises(tmp_path):
    p = tmp_path / "nots.vcd"
    p.write_text("$scope module a $end\n$upscope $end\n$enddefinitions $end\n")
    v = get_vcd(p)
    with pytest.raises(WaveformError):
        format_ticks(1, v)


# --- get_vcd cache ------------------------------------------------------------


def test_get_vcd_caches_by_mtime(tmp_path):
    p = tmp_path / "w.vcd"
    p.write_text(VCD_PATH.read_text(encoding="utf-8"))
    first = get_vcd(p)
    assert get_vcd(p) is first
    st = p.stat()
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 2_000_000_000))
    assert get_vcd(p) is not first


def test_get_vcd_missing_raises(tmp_path):
    with pytest.raises(WaveformError):
        get_vcd(tmp_path / "nope.vcd")


# --- anchor extraction from the test log --------------------------------------


def test_find_anchor_from_log_real():
    secs, msg = find_anchor_from_log(LOG_PATH.read_text(encoding="utf-8"))
    assert secs == Decimal("5e-8")
    assert msg.startswith("expected 99")


def test_find_anchor_from_log_none():
    assert find_anchor_from_log("all fine\nno errors here\n") == (None, "")


# --- signal name resolution -----------------------------------------------------


def test_resolve_signals_dedups_aliases():
    # GHDL dumps count under both tb_counter_fail.count and .dut.count
    v = _fixture_vcd()
    signals, unmatched = resolve_signals(v, ["count"])
    assert unmatched == []
    assert len(signals) == 1
    assert int(signals[0].size) == 8


def test_resolve_signals_multiple_and_unmatched():
    v = _fixture_vcd()
    signals, unmatched = resolve_signals(v, ["count", "inc", "nope"])
    assert unmatched == ["nope"]
    assert len(signals) == 2


def test_resolve_signals_full_name_and_range():
    v = _fixture_vcd()
    signals, _ = resolve_signals(v, ["tb_counter_fail.inc"])
    assert len(signals) == 1
    signals, _ = resolve_signals(v, ["runner"])
    assert len(signals) == 1
    assert int(signals[0].size) == 21


def test_signal_names_bounded():
    names = signal_names(_fixture_vcd(), limit=5)
    lines = names.splitlines()
    assert len(lines) == 6  # 5 names + the "more" note
    assert lines[-1] == "(+ 6 more)"


# --- rendering on the fixture VCD ----------------------------------------------


def test_render_waveform_fixture_failing_check():
    v = _fixture_vcd()
    secs, msg = find_anchor_from_log(LOG_PATH.read_text(encoding="utf-8"))
    anchor = seconds_to_ticks(secs, v)
    assert anchor == 50000000
    signals, _ = resolve_signals(v, ["count", "inc"])
    text = render_waveform(
        v,
        signals,
        test_name=FIXED_TEST,
        anchor_ticks=anchor,
        window_ticks=seconds_to_ticks(Decimal("1e-7"), v),  # 100 ns
        anchor_source=f'failing check: "{msg}"',
    )
    assert f"Waveform for {FIXED_TEST}:" in text
    assert "VCD: 2 unique signal(s) of 11" in text
    assert "Anchor: 50 ns" in text
    assert "Window: 0 ns – 50 ns" in text
    assert "tb_counter_fail.count (8 bits):" in text
    assert "0 ns: 00000000 (0)" in text
    assert "25 ns: 00000001 (1)" in text
    assert "35 ns: 00000010 (2)" in text
    assert "45 ns: 00000011 (3)" in text
    assert "@ 50 ns: 00000011 (3)" in text
    assert "tb_counter_fail.inc (1 bit):" in text
    assert "20 ns: 1" in text
    assert "@ 50 ns: 1" in text


# --- rendering on a hand-rolled VCD --------------------------------------------

SMALL_VCD = """$timescale
  1 ns
$end
$scope module top $end
$var wire 1 a fast $end
$var wire 4 b vec[3:0] $end
$upscope $end
$enddefinitions $end
$dumpvars
#0
0a
b0000 b
$end
#1
1a
#2
0a
#3
1a
#4
0a
#5
1a
#6
0a
#7
b0001 b
#8
1a
#9
bx001 b
$end
"""


def _small_vcd() -> vcdvcd.VCDVCD:
    return vcdvcd.VCDVCD(vcd_string=SMALL_VCD)


def test_render_max_transitions_keeps_closest_to_anchor():
    v = _small_vcd()
    fast = v.data[v.references_to_ids["top.fast"]]
    text = render_waveform(
        v,
        [fast],
        test_name="t",
        anchor_ticks=9,
        window_ticks=20,
        anchor_source="explicit time",
        max_transitions=3,
    )
    # 8 transitions in [0, 9]; only the 3 closest to the anchor, in time order
    assert "5 ns: 1" in text
    assert "6 ns: 0" in text
    assert "8 ns: 1" in text
    assert "1 ns: 1" not in text
    assert "… + 5 transition(s) not shown (raise max_transitions)" in text
    assert "@ 9 ns: 1" in text


def test_render_xz_values_and_vector_decimal():
    v = _small_vcd()
    vec = v.data[v.references_to_ids["top.vec[3:0]"]]
    text = render_waveform(
        v,
        [vec],
        test_name="t",
        anchor_ticks=9,
        window_ticks=20,
        anchor_source="explicit time",
    )
    assert "top.vec (4 bits):" in text
    assert "0 ns: 0000 (0)" in text
    assert "7 ns: 0001 (1)" in text
    assert "9 ns: x001" in text  # x value -> no decimal
    assert "@ 9 ns: x001" in text


def test_render_no_transitions_in_window():
    v = _small_vcd()
    vec = v.data[v.references_to_ids["top.vec[3:0]"]]
    text = render_waveform(
        v,
        [vec],
        test_name="t",
        anchor_ticks=0,
        window_ticks=0,
        anchor_source="explicit time",
    )
    assert "0 ns: 0000 (0)" in text  # t=0 initial value
    assert "@ 0 ns: 0000 (0)" in text
