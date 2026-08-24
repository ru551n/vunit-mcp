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
    "canonical_waveform_format",
    "find_anchor_from_log",
    "find_waveform_file",
    "format_seconds",
    "help_supports_wave_flag",
    "parse_time_str",
    "run_waveform_args",
    "waveform_unavailable_reason",
]

# Formats vunit-mcp knows about. 'fst' (NVC's default with the new --wave
# flag) and 'vcd' are machine-readable; 'ghw' is for the gtkwave GUI.
WAVEFORM_FORMATS = ("vcd", "ghw", "fst")

# Canonical recording format per simulator: the server always records these,
# overriding any other explicit choice (noted in the result). FST is the
# compact machine-readable format external waveform MCPs prefer, and NVC's
# native one; VCD is GHDL's established path. An unknown/undetermined
# simulator keeps the caller's choice.
CANONICAL_WAVEFORM_FORMATS: dict[str, Literal["vcd", "fst"]] = {
    "ghdl": "vcd",
    "nvc": "fst",
}


def canonical_waveform_format(simulator: str | None) -> Literal["vcd", "fst"] | None:
    """Waveform format the server records for ``simulator`` (VCD for GHDL,
    FST for NVC), or None when the simulator is unknown so the caller's
    explicit choice stands."""
    if simulator is None:
        return None
    return CANONICAL_WAVEFORM_FORMATS.get(simulator.strip().lower())

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
    test_dir: Path, fmt: Literal["vcd", "ghw", "fst"] | None = None
) -> Path | None:
    """Waveform file recorded for a test.

    GHDL writes ``<test_dir>/<simulator>/wave.<fmt>``; NVC (once VUnit ships
    headless waveform generation, upstream PR #1101) writes
    ``<test_dir>/nvc/<entity>.<fmt>``. Both naming schemes are matched,
    ``wave.<fmt>`` first. When fmt is omitted, machine-readable formats
    (VCD, then FST) are preferred over GHW. Returns None when no waveform
    of the requested format was recorded.
    """
    prefs = ("vcd", "fst", "ghw") if fmt is None else (fmt,)
    for ext in prefs:
        for pattern in (f"*/wave.{ext}", f"*/*.{ext}"):
            for path in sorted(test_dir.glob(pattern)):
                if path.is_file():
                    return path
    return None


# --wave is the new headless-waveform flag (upstream PR #1101). It is
# matched with a negative lookahead so --wave-fmt/--waves-fmt do not
# trigger a false positive; a future release spelling it --waves (the PR
# title) would still match.
_WAVE_FLAG_RE = re.compile(r"--wave(?!-)")


def help_supports_wave_flag(help_text: str) -> bool:
    """Whether run.py's --help output advertises the new --wave flag."""
    return bool(_WAVE_FLAG_RE.search(help_text))


def waveform_unavailable_reason(
    simulator: str | None, wave_flag_supported: bool
) -> str | None:
    """Why waveforms cannot be recorded headlessly, or None if they can.

    On a VUnit with the new --wave flag (upstream PR #1101) waveforms are
    recorded headlessly for GHDL and NVC alike. On an older VUnit only GHDL
    records headlessly; NVC needs --gui (or the --wave release), so a run
    would silently record nothing. Returns a reason only for that NVC+legacy
    case; None otherwise — including an unknown/auto-detected simulator,
    where we assume the legacy GHDL path.
    """
    if wave_flag_supported:
        return None
    if simulator and simulator.strip().lower() == "nvc":
        return (
            "NVC cannot record waveforms headlessly on this VUnit — it needs "
            "a VUnit with the new --wave flag (upstream PR #1101) or the "
            "interactive --gui. Run without waveform_format, or switch to "
            "GHDL, to record waveforms."
        )
    return None


def run_waveform_args(
    fmt: Literal["vcd", "ghw", "fst"], wave_flag_supported: bool
) -> list[str]:
    """run.py args that make the run record waveforms in ``fmt``.

    VUnit with the new --wave flag (upstream PR #1101) gets
    ``--wave --wave-fmt <fmt>`` — headless for GHDL and NVC. Older VUnit
    gets the legacy ``--gtkwave-fmt <fmt>``: headless only for GHDL, and on
    NVC it is ignored unless --gui is set. fst is therefore only offered via
    the new flag — on a legacy install it would silently record nothing on
    NVC (and GHDL's fst still needs the gtkwave tooling), so we raise rather
    than pretend. Passing --wave to an older VUnit would be an argparse
    error, so the caller must pass the probe result.
    """
    if wave_flag_supported:
        return ["--wave", "--wave-fmt", fmt]
    if fmt == "fst":
        raise ValueError(
            "fst waveforms need a VUnit with the --wave flag (upstream PR "
            "#1101); on this VUnit fst would not be recorded headlessly. "
            "Upgrade VUnit, or use vcd/ghw (GHDL only)."
        )
    return ["--gtkwave-fmt", fmt]
