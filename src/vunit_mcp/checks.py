"""Parse VUnit check-library results from per-test simulator logs.

Verified against VUnit 4.7.1: there is no "VUnit checks: X/Y passed"
summary line in test logs. A *failing* check is logged as an
ERROR-severity report line in the simulator's log format (real GHDL
sample: ``200000000 fs - check - ERROR - this check is deliberately
wrong``); passing checks are silent unless a pass-visible checker is
configured (then INFO "Check passed" lines appear). So failures are
derived from per-simulator severity-line patterns, and pass counts are
only reported when pass lines are visible.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = [
    "CheckHit",
    "count_passed_checks",
    "parse_check_results",
    "render_check_summary",
]

# Severities that indicate a failed check or other report-worthy problem.
_FAILURE_SEVERITIES = ("ERROR", "FAILURE")
_SEVERITIES = r"(?P<sev>ERROR|FAILURE|WARNING|NOTE|INFO)"

# One pattern per simulator family, tried per line in order; first match
# wins. Shapes mirror real check-report lines so simulator-internal noise
# (``ghdl:error:``, ``simulation failed``, VUnit's "Test case ... failed
# after" trailer, ...) never matches:
# - GHDL (VUnit --vunit log format):  <time> <unit> - <process> - <SEV> - <msg>
# - ModelSim/Questa:                  ** <SEV>: <msg>   (optionally '#' prefixed)
# - NVC / iverilog-style:             <time> <unit>? <SEV>: <msg>
_FAMILIES = (
    re.compile(
        r"^\s*(?P<time>[\d.]+\s*\w+)\s+-\s+\S+\s+-\s+"
        + _SEVERITIES + r"\s+-\s+(?P<msg>.+)$"
    ),
    re.compile(r"^\s*#?\s*\*{2,}\s+" + _SEVERITIES + r"\s*:\s*(?P<msg>.+)$"),
    re.compile(
        r"^\s*(?P<time>[\d.]+\s*\w*(?:\s*\(\d+\))?)\s+"
        + _SEVERITIES + r"\s*:\s*(?P<msg>.+)$"
    ),
)

# Pass-visible checker lines log "Check passed (line N)" at INFO severity.
_PASS_RE = re.compile(r"(?i)^\s*check passed\b")


@dataclass(frozen=True)
class CheckHit:
    severity: str
    message: str
    line_no: int  # 1-based line number within the parsed text
    time_str: str = ""


def _match(line: str) -> tuple[str, str, str] | None:
    """Return (severity, message, time_str) for the first family that matches."""
    for rx in _FAMILIES:
        m = rx.match(line)
        if m:
            return (
                m.group("sev"),
                m.group("msg").strip(),
                (m.groupdict().get("time") or "").strip(),
            )
    return None


def parse_check_results(log_text: str) -> list[CheckHit]:
    """Extract failing-check / problem report lines from a test log.

    Pass-visible "Check passed" INFO lines are deliberately excluded
    (count them with :func:`count_passed_checks`); other INFO lines are
    noise and skipped as well.
    """
    hits: list[CheckHit] = []
    for line_no, line in enumerate(log_text.splitlines(), start=1):
        m = _match(line)
        if m is None:
            continue
        severity, message, time_str = m
        if severity in ("NOTE", "INFO"):
            continue
        hits.append(CheckHit(severity, message, line_no, time_str))
    return hits


def count_passed_checks(log_text: str) -> int:
    """Count pass-visible "Check passed" lines (0 when no checker makes
    passes visible — the default)."""
    n = 0
    for line in log_text.splitlines():
        m = _match(line)
        if m and m[0] == "INFO" and _PASS_RE.match(m[1]):
            n += 1
    return n


def render_check_summary(
    hits: list[CheckHit], passed: int = 0, max_shown: int = 10
) -> str:
    """Bounded rendering of parsed hits for tool output.

    Line numbers refer to the parsed (shown) text. Returns "" when there
    is nothing to report, so callers can append it unconditionally.
    """
    if not hits and not passed:
        return ""
    failing = sum(1 for h in hits if h.severity in _FAILURE_SEVERITIES)
    first = f"Check results: {failing} failing check(s)"
    other = len(hits) - failing
    if other:
        first += f", {other} warning(s)"
    if passed:
        first += f", {passed} visible passed check(s)"
    lines = [first]
    for h in hits[:max_shown]:
        when = f" at {h.time_str}" if h.time_str else ""
        lines.append(f"- [{h.severity}] line {h.line_no}{when}: {h.message}")
    if len(hits) > max_shown:
        lines.append(f"  (+ {len(hits) - max_shown} more — see full log)")
    return "\n".join(lines)
