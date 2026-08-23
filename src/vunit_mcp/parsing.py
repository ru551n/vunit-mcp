"""Pure parsers for VUnit run.py outputs (no simulator required).

Verified against VUnit 4.7.1:
- ``--list`` prints one test name per line, then ``Listed N tests``.
- ``test_name_to_path_mapping.txt`` lines are ``<dir-basename> <full_test_name>``.
- JUnit XML (``-x``) holds per-testcase status and ``<system-out>``.
"""

from __future__ import annotations

import importlib.util
import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

_LISTED_RE = re.compile(r"^Listed (\d+) tests?$")


@dataclass(frozen=True)
class TestResult:
    name: str
    classname: str
    time: float
    status: str  # "passed" | "failed" | "error" | "skipped"
    message: str = ""

    @property
    def fullname(self) -> str:
        # JUnit classnames use dots; VUnit test names use dots too.
        if self.classname and not self.name.startswith(self.classname):
            return f"{self.classname}.{self.name}"
        return self.name


@dataclass(frozen=True)
class JUnitReport:
    tests: list[TestResult] = field(default_factory=list)
    time: float = 0.0
    failures: int = 0
    errors: int = 0
    skipped: int = 0

    @property
    def passed(self) -> int:
        return sum(1 for t in self.tests if t.status == "passed")

    @property
    def failed(self) -> list[TestResult]:
        return [t for t in self.tests if t.status in ("failed", "error")]

    def summary(self) -> str:
        lines = [
            (
                f"Tests: {len(self.tests)} | passed: {self.passed} | "
                f"failed: {self.failures + self.errors} | skipped: {self.skipped}"
                f" | time: {self.time:.2f}s"
            )
        ]
        if self.failed:
            lines.append("Failing tests:")
            lines.extend(f"- {t.fullname}" for t in self.failed)
        return "\n".join(lines)


def parse_test_list(stdout: str) -> list[str]:
    """Extract test names from `run.py --list` output."""
    names = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line or _LISTED_RE.match(line):
            continue
        names.append(line)
    return names


def parse_file_list(stdout: str) -> list[str]:
    """Extract files (compile order) from `run.py --files` output.

    Lines look like ``<library>, <path>``; anything else (e.g. the
    ``Listed N files`` footer) is ignored.
    """
    return [ln.strip() for ln in stdout.splitlines() if ", " in ln]


def parse_junit(xml_path: Path) -> JUnitReport:
    """Parse a JUnit XML file into a structured report."""
    tree = ET.parse(xml_path)
    root = tree.getroot()

    # Accept either <testsuite> or <testsuites> as root.
    suites = [root] if root.tag == "testsuite" else root.findall(".//testsuite")

    tests: list[TestResult] = []
    total_time = 0.0
    failures = errors = skipped = 0
    for suite in suites:
        failures += int(suite.get("failures", "0") or 0)
        errors += int(suite.get("errors", "0") or 0)
        skipped += int(suite.get("skipped", "0") or 0)
        case_time = 0.0
        for case in suite.findall("testcase"):
            name = case.get("name", "")
            classname = case.get("classname", "")
            time = float(case.get("time", "0") or 0)
            failure = case.find("failure")
            error = case.find("error")
            skip = case.find("skipped")
            if failure is not None:
                status, msg = "failed", failure.get("message", "") or ""
            elif error is not None:
                status, msg = "error", error.get("message", "") or ""
            elif skip is not None:
                status, msg = "skipped", skip.get("message", "") or ""
            else:
                status, msg = "passed", ""
            case_time += time
            tests.append(TestResult(name, classname, time, status, msg))
        # Some JUnit writers (incl. VUnit) omit the suite time; fall back to
        # the sum of its testcase times.
        suite_time = suite.get("time")
        total_time += float(suite_time) if suite_time else case_time
    return JUnitReport(tests=tests, time=total_time, failures=failures,
                       errors=errors, skipped=skipped)


def _mapping_paths(output_dir: Path) -> list[Path]:
    """Candidate locations for the mapping file.

    VUnit < 4 writes it in the output dir itself; VUnit 4.x nests per-test
    output under ``test_output/``.
    """
    return [
        output_dir / "test_name_to_path_mapping.txt",
        output_dir / "test_output" / "test_name_to_path_mapping.txt",
    ]


def parse_mapping_file(output_dir: Path) -> dict[str, Path]:
    """Map full test name -> per-test output dir from the mapping file.

    Returns {} if the file does not exist yet.
    """
    for mapping_path in _mapping_paths(output_dir):
        if not mapping_path.is_file():
            continue
        base = mapping_path.parent
        mapping: dict[str, Path] = {}
        for line in mapping_path.read_text(encoding="utf-8").splitlines():
            if " " not in line:
                continue
            dir_name, test_name = line.split(" ", 1)
            mapping[test_name.strip()] = base / dir_name
        return mapping
    return {}


def resolve_test_log(output_dir: Path, test_name: str) -> Path | None:
    """Locate the per-test output.txt via the mapping file, or None."""
    test_dir = parse_mapping_file(output_dir).get(test_name)
    if test_dir:
        candidate = test_dir / "output.txt"
        if candidate.is_file():
            return candidate
    return None


def read_tail(path: Path, lines: int | None = None, max_bytes: int = 24_000) -> str:
    """Read a file (or its last N lines) with a hard size cap for safety.

    The cap keeps tool output small enough to hand to an LLM: even the
    "full log" path only ever returns the last ``max_bytes`` of the file.
    Only that cap is read from disk — verbose simulator logs can be many
    MB and we never need more than the tail.
    """
    with path.open("rb") as f:
        f.seek(0, os.SEEK_END)
        f.seek(max(0, f.tell() - max_bytes))
        raw = f.read()
    text = raw.decode(errors="replace")
    if lines is not None:
        return "\n".join(text.splitlines()[-lines:])
    return text


def count_lines(path: Path, max_bytes: int = 1 << 20) -> tuple[int, bool]:
    """Count newlines, scanning at most ``max_bytes`` of the file.

    Returns ``(count, exact)``; ``exact`` is False when the file is longer
    than the cap (count is then a lower bound). Keeps a header note from
    full-scanning multi-GB verbose simulator logs.
    """
    chunk_size = min(1 << 20, max(1, max_bytes))
    n = scanned = 0
    with path.open("rb") as f:
        while scanned < max_bytes:
            chunk = f.read(chunk_size)
            if not chunk:
                return n, True
            n += chunk.count(b"\n")
            scanned += len(chunk)
    return n, False


_ERROR_RE = re.compile(
    r"(?i)\b(error|fatal|assertion\b.*fail|fail(ed|ure)?\b)"
)


def error_excerpt(text: str, max_hits: int = 10) -> str:
    """Extract the error-looking lines from tool output, with 2 lines of
    following context each. Falls back to the tail of the text if nothing
    matches. Keeps failure output small while preserving the actual errors."""
    lines = text.splitlines()
    if not lines:
        return ""
    keep: set[int] = set()
    hits = 0
    for i, line in enumerate(lines):
        if _ERROR_RE.search(line):
            hits += 1
            keep.update(range(i, min(i + 3, len(lines))))
            if hits >= max_hits:
                break
    if not keep:
        return "\n".join(lines[-max_hits * 3 :])
    kept = sorted(keep)
    if len(kept) > max_hits * 3:
        kept = kept[: max_hits * 3]
    out: list[str] = []
    prev: int | None = None
    for i in kept:
        if prev is not None and i > prev + 1:
            out.append("  ...")
        out.append(lines[i])
        prev = i
    return "\n".join(out)


# ANSI escape sequences: CSI (\x1b[...final), OSC (\x1b]...BEL/ST), and
# two-byte Fe/Fn sequences (\x1b=, \x1b>, \x1bM, ...). Simulators like NVC
# emit colored output even when captured through pipes.
_ANSI_RE = re.compile(
    r"\x1b\[[0-9;?]*[ -/]*[@-~]"
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"
    r"|\x1b[\x30-\x7e]"
)


def strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences so captured output is plain text."""
    return _ANSI_RE.sub("", text)


_VUNIT_PKG_DIR: str | None = None
_VUNIT_PKG_DIR_RESOLVED = False


def _vunit_pkg_dir() -> str | None:
    """Directory of the installed vunit package (lazily resolved, cached),
    or None when it cannot be located. Uses find_spec so the vunit package
    is never imported here (keep the server's import light — see
    project_model for the same policy)."""
    global _VUNIT_PKG_DIR, _VUNIT_PKG_DIR_RESOLVED
    if not _VUNIT_PKG_DIR_RESOLVED:
        spec = importlib.util.find_spec("vunit")
        locs = list(getattr(spec, "submodule_search_locations", None) or [])
        _VUNIT_PKG_DIR = os.path.normpath(str(locs[0])) if locs else None
        _VUNIT_PKG_DIR_RESOLVED = True
    return _VUNIT_PKG_DIR


def is_vunit_builtin(path: str) -> bool:
    """True if a file path is inside the installed VUnit package (built-in
    library source) rather than the user's project. Built-ins are stable
    installed files, so tools summarize them instead of listing them.

    Matches against the real installed vunit package directory (a project
    subdir merely named ``vunit/`` is not a built-in); falls back to a
    ``/vunit/`` path heuristic only when the package cannot be located.
    """
    norm = path.replace("\\", "/")
    pkg = _vunit_pkg_dir()
    if pkg is not None:
        return norm.startswith(pkg.replace("\\", "/") + "/")
    return "/vunit/" in norm


def find_simulator_error(stdout: str, stderr: str) -> str | None:
    """Detect VUnit's 'no simulator' error so the tool can say so clearly."""
    combined = f"{stdout}\n{stderr}"
    for marker in ("No available simulator", "No simulator found"):
        if marker in combined:
            for line in combined.splitlines():
                if marker in line:
                    return line.strip()
    return None
