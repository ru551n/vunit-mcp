"""Waveform resolution for vunit_get_test_waveform.

vunit-mcp does not parse waveforms itself: it locates the waveform file that
vunit_run_tests recorded for a test (VCD for MCP-based analysis, GHW for the
gtkwave GUI) and reports its path plus, when available, the simulation time
of the test's failing check. Reading the waveform is left to a separate
waveform-reading MCP server that receives the path.
"""

from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Literal

from .checks import parse_check_results

__all__ = [
    "find_anchor_from_log",
    "find_waveform_file",
    "format_seconds",
    "parse_time_str",
]

_SECONDS_PER_UNIT = {
    "fs": Decimal("1e-15"),
    "ps": Decimal("1e-12"),
    "ns": Decimal("1e-9"),
    "us": Decimal("1e-6"),
    "ms": Decimal("1e-3"),
    "s": Decimal(1),
}

_TIME_RE = re.compile(
    r"^\s*([0-9]*\.?[0-9]+)\s*(fs|ps|ns|us|µs|ms|s)\s*$", re.IGNORECASE
)

_DISPLAY_UNITS = (
    ("s", Decimal(1)),
    ("ms", Decimal(10) ** 3),
    ("us", Decimal(10) ** 6),
    ("ns", Decimal(10) ** 9),
    ("ps", Decimal(10) ** 12),
    ("fs", Decimal(10) ** 15),
)

_UNIT_DIVISOR = dict(_DISPLAY_UNITS)


def parse_time_str(text: str) -> Decimal | None:
    """Parse '50 ns' / '50000000 fs' / '1us' into seconds (Decimal).

    Returns None when the text is not '<number> <unit>' with a time unit.
    """
    m = _TIME_RE.match(text)
    if not m:
        return None
    unit = m.group(2).lower()
    if unit == "µs":
        unit = "us"
    return Decimal(m.group(1)) * _SECONDS_PER_UNIT[unit]


def format_seconds(seconds: Decimal) -> str:
    """Render a duration in seconds in the largest unit where it is >= 1."""
    unit = "s"
    for u, div in _DISPLAY_UNITS:
        if seconds * div >= 1:
            unit = u
            break
    value = (seconds * _UNIT_DIVISOR[unit]).quantize(
        Decimal("0.001"), rounding=ROUND_HALF_UP
    )
    if value == value.to_integral_value():
        return f"{int(value)} {unit}"
    return f"{value.normalize()} {unit}"


def find_anchor_from_log(log_text: str) -> tuple[Decimal | None, str]:
    """Time (seconds) + message of the first ERROR/FAILURE check in a log.

    Uses checks.parse_check_results, which already extracts the per-simulator
    timestamp (e.g. '50000000 fs' from a GHDL check line). Returns
    (None, "") when the log has no dated failing check.
    """
    for hit in parse_check_results(log_text):
        if hit.severity not in ("ERROR", "FAILURE") or not hit.time_str:
            continue
        seconds = parse_time_str(hit.time_str)
        if seconds is not None:
            return seconds, hit.message
    return None, ""


def find_waveform_file(
    test_dir: Path, fmt: Literal["vcd", "ghw"] | None = None
) -> Path | None:
    """Waveform file recorded for a test: <test_dir>/<simulator>/wave.<fmt>.

    When fmt is omitted, VCD is preferred over GHW. Returns None when no
    waveform of the requested format was recorded.
    """
    prefs = ("vcd", "ghw") if fmt is None else (fmt,)
    for ext in prefs:
        for path in sorted(test_dir.glob(f"*/wave.{ext}")):
            if path.is_file():
                return path
    return None
