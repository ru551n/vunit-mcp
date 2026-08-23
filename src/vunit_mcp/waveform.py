"""VCD waveform inspection for vunit_get_test_waveform (via vcdvcd).

Pure synchronous core; the server wraps the (CPU-bound) parse in a thread.
v1 targets small VCDs: the whole file is loaded with ``vcdvcd.VCDVCD``.
Larger files can later use vcdvcd's ``to_time``/``signal_res``/callbacks
streaming options without changing this module's public API.

Verified against GHDL VCD output (VUnit --gtkwave-fmt vcd, VUnit 4.7.1):
vector values are MSB-first, so they display as-is; signals are dumped per
process scope, so the same net appears under several names (aliases) which
are de-duplicated here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import vcdvcd  # type: ignore[import-untyped]

from .checks import parse_check_results

__all__ = [
    "WaveformError",
    "clear_vcd_cache",
    "find_anchor_from_log",
    "format_ticks",
    "get_vcd",
    "parse_time_str",
    "render_waveform",
    "resolve_signals",
    "seconds_to_ticks",
    "signal_names",
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

_RANGE_RE = re.compile(r"\[.*\]$")

_DISPLAY_UNITS = (
    ("s", Decimal(1)),
    ("ms", Decimal(10) ** 3),
    ("us", Decimal(10) ** 6),
    ("ns", Decimal(10) ** 9),
    ("ps", Decimal(10) ** 12),
    ("fs", Decimal(10) ** 15),
)


class WaveformError(RuntimeError):
    """User-facing error when locating/parsing/rendering a waveform."""


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


def _ts_seconds(vcd: vcdvcd.VCDVCD) -> Decimal:
    """Seconds per VCD tick from the file's $timescale (Decimal)."""
    ts = vcd.timescale.get("timescale")
    if not ts:
        raise WaveformError(f"{vcd}: no $timescale found — cannot convert times")
    return Decimal(ts)


def seconds_to_ticks(seconds: Decimal, vcd: vcdvcd.VCDVCD) -> int:
    """Convert a duration in seconds to VCD ticks (rounded to nearest)."""
    return int((seconds / _ts_seconds(vcd)).to_integral_value(rounding=ROUND_HALF_UP))


_UNIT_DIVISOR = dict(_DISPLAY_UNITS)


def _unit_for(ticks: int, vcd: vcdvcd.VCDVCD) -> str:
    """Largest display unit where ticks >= 1 (timescale unit for 0/tiny)."""
    secs = Decimal(ticks) * _ts_seconds(vcd)
    unit = vcd.timescale.get("unit", "fs")
    for u, div in _DISPLAY_UNITS:
        if secs * div >= 1:
            unit = u
            break
    return unit


def format_ticks(
    ticks: int, vcd: vcdvcd.VCDVCD, unit: str | None = None
) -> str:
    """Render VCD ticks in the given unit, or the largest one where >= 1."""
    secs = Decimal(ticks) * _ts_seconds(vcd)
    if unit is None:
        unit = _unit_for(ticks, vcd)
    value = secs * _UNIT_DIVISOR[unit]
    q = value.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)
    if q == q.to_integral_value():
        return f"{int(q)} {unit}"
    return f"{q.normalize()} {unit}"


@dataclass(frozen=True)
class _CacheEntry:
    mtime: float
    vcd: vcdvcd.VCDVCD


_VCD_CACHE: dict[Path, _CacheEntry] = {}
_MAX_CACHED_VCDS = 8


def get_vcd(path: Path) -> vcdvcd.VCDVCD:
    """Load and cache a parsed VCD, keyed by (path, mtime).

    Re-parses when the file changes; evicts oldest entries beyond the cap.
    """
    try:
        mtime = path.stat().st_mtime
    except OSError as exc:
        raise WaveformError(f"cannot stat {path}: {exc}") from exc
    entry = _VCD_CACHE.get(path)
    if entry is not None and entry.mtime == mtime:
        return entry.vcd
    try:
        vcd = vcdvcd.VCDVCD(str(path))
    except Exception as exc:
        raise WaveformError(f"failed to parse {path}: {exc}") from exc
    if path not in _VCD_CACHE and len(_VCD_CACHE) >= _MAX_CACHED_VCDS:
        _VCD_CACHE.pop(next(iter(_VCD_CACHE)))
    _VCD_CACHE[path] = _CacheEntry(mtime, vcd)
    return vcd


def clear_vcd_cache() -> None:
    """Drop all cached VCDs (used by tests)."""
    _VCD_CACHE.clear()


def _base_name(ref: str) -> str:
    """Reference name without its trailing range ('a.count[7:0]' -> 'a.count')."""
    return _RANGE_RE.sub("", ref)


def _short_ref(sig: vcdvcd.Signal) -> str:
    return min(sig.references, key=len) if sig.references else "?"


def _dedup(signals: list[vcdvcd.Signal]) -> list[vcdvcd.Signal]:
    """Drop alias nets (GHDL dumps the same net per process scope)."""
    seen: set[tuple] = set()
    out: list[vcdvcd.Signal] = []
    for sig in signals:
        key = (sig.size, tuple(sig.tv))
        if key in seen:
            continue
        seen.add(key)
        out.append(sig)
    return out


def resolve_signals(
    vcd: vcdvcd.VCDVCD, queries: list[str]
) -> tuple[list[vcdvcd.Signal], list[str]]:
    """Resolve name queries to Signal objects.

    A query matches a signal whose full name (range stripped) equals it,
    ends with '.' + query, or whose leaf element equals it. Alias nets with
    identical values are de-duplicated (shortest name wins). Returns
    (signals, unmatched_queries) — queries keep their input order.
    """
    out: list[vcdvcd.Signal] = []
    seen_keys: set[tuple] = set()
    unmatched: list[str] = []
    for q in queries:
        matches = _match_query(vcd, q)
        if not matches:
            unmatched.append(q)
            continue
        for sig in matches:
            key = (sig.size, tuple(sig.tv))
            if key in seen_keys:
                continue
            seen_keys.add(key)
            out.append(sig)
    return out, unmatched


def _match_query(vcd: vcdvcd.VCDVCD, query: str) -> list[vcdvcd.Signal]:
    out: list[vcdvcd.Signal] = []
    for ref, sid in vcd.references_to_ids.items():
        base = _base_name(ref)
        leaf = base.rsplit(".", 1)[-1]
        if query == base or query == leaf or base.endswith("." + query):
            out.append(vcd.data[sid])
    out.sort(key=lambda s: len(_short_ref(s)))
    return out


def signal_names(vcd: vcdvcd.VCDVCD, limit: int = 50) -> str:
    """All signal names (file order) as a bounded list for error hints."""
    names = list(vcd.signals)
    text = "\n".join(f"- {n}" for n in names[:limit])
    if len(names) > limit:
        text += f"\n(+ {len(names) - limit} more)"
    return text


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


def _size(sig: vcdvcd.Signal) -> int:
    # vcdvcd keeps $var sizes as strings.
    return int(sig.size)


def _format_value(sig: vcdvcd.Signal, value: str) -> str:
    if len(value) > 64:
        return f"{value[:16]}…{value[-16:]} ({len(value)} bits)"
    if _size(sig) == 1:
        return value
    if set(value) <= {"0", "1"}:
        return f"{value} ({int(value, 2)})"
    return value


def render_waveform(
    vcd: vcdvcd.VCDVCD,
    signals: list[vcdvcd.Signal],
    *,
    test_name: str,
    anchor_ticks: int,
    window_ticks: int,
    anchor_source: str,
    max_transitions: int = 100,
    max_signals: int = 40,
    max_bytes: int = 24_000,
) -> str:
    """Bounded, LLM-friendly rendering of a transition window + snapshot.

    Per signal: value changes within [anchor-window, anchor+window] (only
    the max_transitions closest to the anchor when there are more), then a
    snapshot line at the anchor time.
    """
    lo = max(vcd.begintime, anchor_ticks - window_ticks)
    hi = min(vcd.endtime, anchor_ticks + window_ticks)
    # One display unit for the whole render, picked from the window end, so
    # "0 ns – 50 ns" never mixes units.
    unit = _unit_for(hi, vcd)
    ft = lambda t: format_ticks(t, vcd, unit)

    unique = _dedup(signals)
    header = [
        f"Waveform for {test_name}:",
        (
            f"VCD: {len(unique)} unique signal(s) of {len(vcd.signals)} "
            f"({ft(vcd.begintime)} – {ft(vcd.endtime)}"
            f", timescale {vcd.timescale['magnitude']} {vcd.timescale['unit']})"
        ),
        f"Anchor: {ft(anchor_ticks)} ({anchor_source})",
        f"Window: {ft(lo)} – {ft(hi)}",
        "",
    ]

    lines = list(header)
    shown = 0
    for sig in unique[:max_signals]:
        block = _render_signal(vcd, sig, lo, hi, anchor_ticks, max_transitions, unit)
        if sum(len(l) + 1 for l in block) + len("\n".join(lines)) > max_bytes:
            break
        lines.extend(block)
        shown += 1
    hidden = len(unique) - shown
    if hidden > 0:
        lines.append(
            f"… (+ {hidden} more signal(s) — re-run with signals=[...] to "
            "narrow the list)"
        )
    return "\n".join(lines)


def _render_signal(
    vcd: vcdvcd.VCDVCD,
    sig: vcdvcd.Signal,
    lo: int,
    hi: int,
    anchor_ticks: int,
    max_transitions: int,
    unit: str,
) -> list[str]:
    name = _base_name(_short_ref(sig))
    size = _size(sig)
    block = [f"{name} ({size} bit{'s' if size != 1 else ''}):"]
    in_window = [(t, v) for t, v in sig.tv if lo <= t <= hi]
    if len(in_window) > max_transitions:
        in_window = sorted(
            in_window, key=lambda tv: abs(tv[0] - anchor_ticks)
        )[:max_transitions]
        in_window.sort()
        omitted = len(sig.tv) - max_transitions
        block.append(f"  … + {omitted} transition(s) not shown (raise max_transitions)")
    for t, v in in_window:
        block.append(f"  {format_ticks(t, vcd, unit)}: {_format_value(sig, v)}")
    snapshot = sig[anchor_ticks]
    if snapshot is None:
        snapshot = "x"
    block.append(
        f"  @ {format_ticks(anchor_ticks, vcd, unit)}: {_format_value(sig, snapshot)}"
    )
    return block
