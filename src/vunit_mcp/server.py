"""FastMCP server exposing a VUnit project as MCP tools.

The server shells out to the project's run.py (VUnit has no standalone CLI,
and VUnit.main() calls sys.exit(), so the server never *runs* vunit
in-process; the deliberate exception is vunit_test_dependencies, which
builds an in-process project model — see project_model).
"""

from __future__ import annotations

import asyncio
import json
import shutil
import time
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from .checks import count_passed_checks, parse_check_results, render_check_summary
from .config import Config, ConfigError, load_config
from .export_cache import get_export_json
from .models import (
    GetTestLogInput,
    GetTestWaveformInput,
    RunTestsInput,
    TestDependenciesInput,
)
from .parsing import (
    JUnitReport,
    count_lines,
    error_excerpt,
    find_simulator_error,
    is_vunit_builtin,
    parse_file_list,
    parse_junit,
    parse_mapping_file,
    parse_test_list,
    read_tail,
    resolve_test_log,
)
from .project_model import InternalProject, InternalProjectError
from .runner import (
    RunTimeoutError,
    resolve_junit_path,
    run_subprocess_sync,
    run_vunit,
)
from .waveform import (
    WaveformError,
    find_anchor_from_log,
    format_ticks,
    get_vcd,
    parse_time_str,
    render_waveform,
    resolve_signals,
    seconds_to_ticks,
    signal_names,
)

mcp = FastMCP(
    "vunit_mcp",
    instructions=(
        "Drive a VUnit (HDL unit-testing) project: list tests, compile, run "
        "tests, and inspect results/logs. Start with vunit_status if anything "
        "is unclear. Test names look like lib.entity[.test_case]. "
        "vunit_test_dependencies answers 'which files do I need to implement "
        "this test?'"
    ),
)

_config: Config | None = None


def get_config() -> Config:
    global _config
    if _config is None:
        _config = load_config()
    return _config


def _err(exc: Exception) -> str:
    """Render a config/timeout error as an actionable tool result."""
    if isinstance(exc, ConfigError):
        return f"Configuration error: {exc}"
    return f"Error: {exc}"


def _short_tail(text: str, lines: int = 10) -> str:
    """Last N lines of raw output, for fallbacks where parsing found nothing."""
    return "\n".join(text.strip().splitlines()[-lines:])


def _failing_checks(output_dir: Path, test_name: str) -> int:
    """Count ERROR/FAILURE check lines in a test's log (0 if no log).

    VUnit stops the simulation on the first check error, so failures sit
    within read_tail's 24 KB cap.
    """
    log_path = resolve_test_log(output_dir, test_name)
    if not log_path:
        return 0
    try:
        tail = read_tail(log_path)
    except OSError:
        return 0
    return sum(
        1
        for h in parse_check_results(tail)
        if h.severity in ("ERROR", "FAILURE")
    )


SIMULATORS = ("ghdl", "nvc", "vsim", "rival", "activehdl", "mti", "incisive")


def _no_simulator_message(sim: str) -> str:
    return (
        f"No simulator available to VUnit. It reported:\n  {sim}\n"
        "Install a simulator (e.g. ghdl or nvc) or set "
        "VUNIT_MCP_SIMULATOR to a VUnit-supported simulator name."
    )


async def _probe(
    config: Config, args: list[str], timeout: float | None = None
) -> tuple[str | None, str | None]:
    """Run run.py. Returns (error_message, stdout); exactly one is None."""
    try:
        result = await run_vunit(config, args, timeout=timeout)
    except (RunTimeoutError, ConfigError) as exc:
        return _err(exc), None
    if not result.ok:
        sim = find_simulator_error(result.stdout, result.stderr)
        if sim:
            return _no_simulator_message(sim), None
        return f"run.py failed (exit {result.returncode}):\n{result.summary()}", None
    return None, result.stdout


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
async def vunit_status() -> str:
    """Report server configuration: project dir, run script, interpreter,
    VUnit version, and whether a simulator appears available. Call this
    first when diagnosing setup problems."""
    try:
        config = get_config()
    except ConfigError as exc:
        return _err(exc)

    vunit_version = "not found"
    try:
        probe = run_subprocess_sync(
            config,
            [
                config.python,
                "-c",
                "import vunit; print(vunit.__version__)",
            ],
        )
        vunit_version = probe.stdout.strip() or f"error: {probe.stderr.strip()}"
    except RunTimeoutError as exc:
        vunit_version = f"probe failed: {exc}"

    sims = [s for s in SIMULATORS if shutil.which(s)]
    if config.simulator:
        sims_note = f"VUNIT_MCP_SIMULATOR={config.simulator} (passthrough)"
    elif sims:
        sims_note = "on PATH: " + ", ".join(sims)
    else:
        sims_note = (
            "none detected on PATH — compile/run tools will fail until a "
            "simulator (ghdl, nvc, vsim, ...) is installed or VUNIT_MCP_SIMULATOR is set"
        )

    return "\n".join(
        [
            "vunit-mcp status",
            f"- project dir : {config.project_dir}",
            f"- run script  : {config.run_script}",
            f"- interpreter : {config.python}",
            f"- vunit       : {vunit_version}",
            f"- simulator   : {sims_note}",
            f"- output dir  : {config.output_dir}",
            f"- timeout     : {config.timeout:.0f}s",
        ]
    )


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
async def vunit_list_tests() -> str:
    """List all test cases (lib.entity[.test_case]) the project knows about.
    Does not require a simulator."""
    try:
        config = get_config()
    except ConfigError as exc:
        return _err(exc)
    err, out = await _probe(config, ["--list"])
    if err or out is None:
        return err or "empty output"
    names = parse_test_list(out)
    if not names:
        return "No tests found (run.py --list returned no test names).\n" + _short_tail(out)
    return f"{len(names)} tests:\n" + "\n".join(f"- {n}" for n in names)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
async def vunit_list_files() -> str:
    """List all source files in compile order. Does not require a simulator."""
    try:
        config = get_config()
    except ConfigError as exc:
        return _err(exc)
    err, out = await _probe(config, ["--files"])
    if err or out is None:
        return err or "empty output"
    files = parse_file_list(out)
    if not files:
        return "No files listed.\n" + _short_tail(out)
    project = [f for f in files if not is_vunit_builtin(f)]
    builtins = len(files) - len(project)
    text = (
        f"{len(project)} project file(s) (compile order):\n"
        + "\n".join(project)
    )
    if builtins:
        text += (
            f"\n(+ {builtins} VUnit built-in library files omitted — they "
            "come from the installed vunit package, not the project)"
        )
    return text


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=False, idempotentHint=True, openWorldHint=False
    )
)
async def vunit_compile() -> str:
    """Compile all sources in the VUnit project (--compile). Requires a
    simulator. Safe to re-run."""
    try:
        config = get_config()
    except ConfigError as exc:
        return _err(exc)
    try:
        result = await run_vunit(config, ["--compile"])
    except RunTimeoutError as exc:
        return _err(exc)
    sim = find_simulator_error(result.stdout, result.stderr)
    if sim:
        return f"No simulator available to VUnit. It reported:\n  {sim}"
    if result.ok:
        # Success output is mostly per-file progress; keep it to a short tail.
        return "Compile succeeded.\n" + _short_tail(result.summary(), 10)
    return "Compile failed:\n" + error_excerpt(result.summary())


def _run_args(input: RunTestsInput, output_dir: Path) -> list[str]:
    args = ["-x", str(output_dir / "junit.xml")]
    if input.num_threads:
        args += ["-p", str(input.num_threads)]
    if input.clean:
        args.append("--clean")
    if input.verbose:
        args.append("--verbose")
    if input.fail_fast:
        args.append("--fail-fast")
    if input.with_attributes:
        args.append("--with-attributes")
    if input.without_attributes:
        args.append("--without-attributes")
    if input.waveform_format:
        args += ["--gtkwave-fmt", input.waveform_format]
    args += input.test_patterns
    return args


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, openWorldHint=False))
async def vunit_run_tests(
    input: RunTestsInput = RunTestsInput(),  # noqa: B008 (FastMCP pattern)
) -> str:
    """Run VUnit tests and return a pass/fail summary plus the list of
    failing tests. Patterns default to ['*'] (run everything). A JUnit XML
    is always written next to the output dir for vunit_get_report.
    Requires a simulator. Set waveform_format='vcd' (GHDL) to record
    waveforms so vunit_get_test_waveform can inspect signal behavior."""
    try:
        config = get_config()
    except ConfigError as exc:
        return _err(exc)
    output_dir = (
        Path(input.output_dir).expanduser().resolve()
        if input.output_dir
        else config.output_dir
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    start = time.time()
    try:
        result = await run_vunit(
            config,
            ["-o", str(output_dir), *_run_args(input, output_dir)],
            timeout=input.timeout,
        )
    except RunTimeoutError as exc:
        return _err(exc)

    sim = find_simulator_error(result.stdout, result.stderr)
    if sim:
        return (
            f"No simulator available to VUnit. It reported:\n  {sim}\n"
            "Install a simulator or set VUNIT_MCP_SIMULATOR."
        )

    report_path = await resolve_junit_path(output_dir)
    # VUnit leaves the JUnit file untouched when it runs no tests (e.g. a
    # pattern matched nothing); never report a file older than this run.
    if (
        report_path is not None
        and report_path.is_file()
        and report_path.stat().st_mtime >= start
    ):
        try:
            report = parse_junit(report_path)
            if not report.tests:
                return (
                    "No tests were run — none of the patterns matched any test. "
                    "Use vunit_list_tests to see available names.\n"
                    + result.summary()
                )
            status = "FAILED" if (not result.ok or report.failed) else "PASSED"
            out = (
                f"Run {status}.\n{report.summary()}\n"
                f"JUnit: {report_path}\n"
                f"Logs: {output_dir} (use vunit_get_test_log for details)"
            )
            if input.waveform_format == "vcd" and report.failed:
                out += (
                    "\nWaveforms recorded — for a failing test, call "
                    "vunit_get_test_waveform(test_name) for a signal-level "
                    "view around the failing check."
                )
            return out
        except Exception as exc:  # noqa: BLE001 — malformed XML, fall back to raw output
            return f"Run finished (exit {result.returncode}) but JUnit parse failed: {exc}\n{result.summary()}"
    if report_path and report_path.is_file():
        return (
            f"Run finished (exit {result.returncode}) but no fresh JUnit was written "
            f"(a stale {report_path} from an earlier run was ignored).\n"
            + result.summary()
        )
    return (
        f"Run finished with exit code {result.returncode} (no JUnit file found in {output_dir}).\n"
        + result.summary()
    )


async def _load_report(config: Config) -> JUnitReport | str:
    report_path = await resolve_junit_path(config.output_dir)
    if not report_path or not report_path.is_file():
        return (
            f"No JUnit report found in {config.output_dir}. Run vunit_run_tests first."
        )
    try:
        return parse_junit(report_path)
    except Exception as exc:  # noqa: BLE001 — malformed report, report and move on
        return f"Failed to parse {report_path}: {exc}"


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
async def vunit_get_report() -> str:
    """Answers "which tests passed/failed in the last run?" — the run-wide
    overview. Re-reads the last run's JUnit XML from the output dir: fast,
    no simulation, no re-run, safe to call repeatedly. Returns every test's
    status, plus the number of failing VUnit checks for each failing test.
    Do NOT use this for details — pick a failing test and call
    vunit_get_test_log on it to see WHY it failed."""
    try:
        config = get_config()
    except ConfigError as exc:
        return _err(exc)
    report = await _load_report(config)
    if isinstance(report, str):
        return report
    lines = [report.summary(), "", "Per-test:"]
    for t in report.tests:
        line = f"- [{t.status.upper()}] {t.fullname} ({t.time:.3f}s)"
        # Only failing tests can have failing checks; skip the log read
        # entirely for everything else.
        if t.status in ("failed", "error"):
            n = _failing_checks(config.output_dir, t.fullname)
            if n:
                line += f" — {n} failing check(s)"
        lines.append(line)
        if t.message:
            lines.append(f"    {t.message}")
    return "\n".join(lines)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
async def vunit_get_test_log(input: GetTestLogInput) -> str:
    """Answers "why did this one test fail?" — the raw output of a single
    test (its output.txt). Use only for a specific test: test_name is the
    full name from vunit_list_tests or a failing test from
    vunit_get_report. For run-wide questions (which tests failed) use
    vunit_get_report instead. Returns the last 100 lines by default
    (failure info appears at the end); pass a larger `lines` for more
    context. When the log contains failing VUnit checks, a structured
    "Check results" section is appended after the log."""
    try:
        config = get_config()
    except ConfigError as exc:
        return _err(exc)
    log_path = resolve_test_log(config.output_dir, input.test_name)
    if log_path is None:
        known = sorted(parse_mapping_file(config.output_dir))
        hint = ""
        if known:
            hint = (
                "\nKnown tests (last run):\n" + "\n".join(f"- {n}" for n in known)
            )
        return f"No log found for test {input.test_name!r} in {config.output_dir}.{hint}"
    shown = input.lines or 0
    text = read_tail(log_path, shown if shown else None)
    total = count_lines(log_path)
    header = f"Log for {input.test_name} ({log_path})"
    if total > shown:
        header += f" — showing last {shown} of {total} lines; raise `lines` for more"
    out = f"{header}:\n---\n{text}"
    summary = render_check_summary(
        parse_check_results(text), count_passed_checks(text)
    )
    if summary:
        out += f"\n\n{summary}\n(line numbers refer to the log shown above)"
    return out


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
async def vunit_get_test_waveform(input: GetTestWaveformInput) -> str:
    """Answers "what were the signals doing when this test failed?" — reads
    the test's VCD waveform (recorded by vunit_run_tests with
    waveform_format='vcd', GHDL only). Anchors on the failing check's time
    from the test log by default and renders a compact per-signal transition
    trace plus a snapshot at the anchor time. Pass signals=['name', ...] to
    focus on specific signals (name or suffix). No re-simulation."""
    try:
        config = get_config()
    except ConfigError as exc:
        return _err(exc)

    mapping = parse_mapping_file(config.output_dir)
    test_dir = mapping.get(input.test_name)
    if test_dir is None:
        known = sorted(mapping)
        hint = ""
        if known:
            hint = "\nKnown tests (last run):\n" + "\n".join(f"- {n}" for n in known)
        return (
            f"No waveform data for test {input.test_name!r} in "
            f"{config.output_dir}.{hint}"
        )

    vcd_path = test_dir / "ghdl" / "wave.vcd"
    if not vcd_path.is_file():
        return (
            f"No waveform for {input.test_name}: {vcd_path} not found — the "
            "test was run without waveform recording. Run vunit_run_tests "
            'with waveform_format="vcd" (GHDL) and call this tool again.'
        )

    try:
        if input.time is not None:
            anchor_secs = parse_time_str(input.time)
            if anchor_secs is None:
                return (
                    f"Invalid time {input.time!r}: expected '<number> <unit>' "
                    "with unit in s/ms/us/ns/ps/fs (e.g. '50 ns')."
                )
            anchor_source = "explicit time"
        else:
            anchor_secs, msg = (None, "")
            log_path = test_dir / "output.txt"
            if log_path.is_file():
                anchor_secs, msg = find_anchor_from_log(read_tail(log_path))
            if anchor_secs is not None:
                anchor_source = f'failing check: "{msg[:120]}"'
            else:
                anchor_source = "no failing check in the log — end of simulation"

        window_text = input.window if input.window is not None else "100 ns"
        window_secs = parse_time_str(window_text)
        if window_secs is None:
            return (
                f"Invalid window {window_text!r}: expected '<number> <unit>' "
                "with unit in s/ms/us/ns/ps/fs (e.g. '100 ns')."
            )

        # Parse off the event loop (even "small" VCDs take milliseconds).
        vcd = await asyncio.to_thread(get_vcd, vcd_path)

        anchor_ticks = (
            vcd.endtime
            if anchor_secs is None
            else seconds_to_ticks(anchor_secs, vcd)
        )
        anchor_ticks = min(max(anchor_ticks, vcd.begintime), vcd.endtime)
        window_ticks = max(0, seconds_to_ticks(window_secs, vcd))
        lo = max(vcd.begintime, anchor_ticks - window_ticks)
        hi = min(vcd.endtime, anchor_ticks + window_ticks)

        if input.signals:
            signals, unmatched = resolve_signals(vcd, input.signals)
            if unmatched:
                return (
                    f"No signal(s) match: {', '.join(unmatched)}.\n"
                    "Available signals:\n" + signal_names(vcd)
                )
        else:
            signals = []
            for ref in vcd.signals:
                sig = vcd.data[vcd.references_to_ids[ref]]
                if any(lo <= t <= hi for t, _ in sig.tv):
                    signals.append(sig)
            if not signals:
                return (
                    f"No signal transitions within {format_ticks(lo, vcd)} – "
                    f"{format_ticks(hi, vcd)} around the anchor. Widen the "
                    "window (e.g. window='1 us') or pass an explicit time."
                )

        text = render_waveform(
            vcd,
            signals,
            test_name=input.test_name,
            anchor_ticks=anchor_ticks,
            window_ticks=window_ticks,
            anchor_source=anchor_source,
            max_transitions=input.max_transitions,
        )
        return f"VCD: {vcd_path}\n{text}"
    except WaveformError as exc:
        return str(exc)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
async def vunit_export_json() -> str:
    """Export the project model (source files, all tests, attributes) as
    JSON via --export-json. Attributes carry requirement/traceability data.
    The export is cached at .vunit-mcp-cache/export.json in the project and
    re-run only when the project's sources change. Does not require a
    simulator."""
    try:
        config = get_config()
    except ConfigError as exc:
        return _err(exc)
    outcome = await get_export_json(config)
    if outcome.error or outcome.data is None:
        return outcome.error or "empty output"
    cache_note = (" (cached — project unchanged since last export)" if outcome.reused else "")
    data = outcome.data
    # Keep the response bounded: counts + names if larger than the cap.
    files = data.get("files", [])
    tests = data.get("tests", [])
    rendered = json.dumps(data, indent=2)
    if len(rendered) > 8_000:
        names = [t["name"] for t in tests]
        project_files = [
            f"{f['file_name']} (lib: {f['library_name']})"
            for f in files
            if not is_vunit_builtin(f["file_name"])
        ]
        builtins = len(files) - len(project_files)
        text = (
            f"Export: {len(files)} files, {len(tests)} tests{cache_note}.\n\n"
            f"Project files (compile order):\n"
            + "\n".join(project_files)
        )
        if builtins:
            text += f"\n(+ {builtins} VUnit built-in library files omitted)"
        text += (
            "\n\nTest names:\n" + "\n".join(names)
            + f"\n\nFull JSON (with attributes): {outcome.path}"
        )
        return text
    return rendered + cache_note


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
async def vunit_test_dependencies(input: TestDependenciesInput) -> str:
    """Return the ordered list of source files needed to implement one
    test case: the files it depends on to elaborate, grouped by library
    in compile order (VUnit built-in files summarized as a count). Does
    not compile and needs no simulator."""
    try:
        config = get_config()
    except ConfigError as exc:
        return _err(exc)
    outcome = await get_export_json(config)
    if outcome.error or outcome.data is None:
        return outcome.error or "empty output"
    data = outcome.data
    try:
        # Off the event loop: the first call parses all project sources in
        # process, which can take seconds-to-minutes on a real project.
        project, model_reused = await asyncio.to_thread(InternalProject.load, config, data)
        matches = project.resolve_test(input.test_name)
        if not matches:
            names = project.test_names
            listed = names[:50]
            msg = (
                f"No test matches {input.test_name!r}.\n"
                f"Available tests ({len(names)}):\n"
                + "\n".join(f"- {n}" for n in listed)
            )
            if len(names) > 50:
                msg += f"\n(+ {len(names) - 50} more)"
            return msg
        if len(matches) > 1:
            names = [t["name"] for t in matches]
            listed = names[:50]
            msg = (
                f"Pattern {input.test_name!r} matches {len(names)} tests — "
                "pass an exact name:\n" + "\n".join(f"- {n}" for n in listed)
            )
            if len(names) > 50:
                msg += f"\n(+ {len(names) - 50} more)"
            return msg

        subset = await asyncio.to_thread(project.implementation_subset, matches[0])
        by_lib: dict[str, list[str]] = {}
        for lib, path in subset:
            by_lib.setdefault(lib, []).append(path)
        project_files = sum(1 for _, p in subset if not is_vunit_builtin(p))
        builtins = len(subset) - project_files

        reused_parts = []
        if outcome.reused:
            reused_parts.append("export")
        if model_reused:
            reused_parts.append("project model")
        cache_note = (
            f" ({' and '.join(reused_parts)} reused from cache)" if reused_parts else ""
        )

        lines = [
            (
                f"Files needed to implement {matches[0]['name']}{cache_note} "
                f"(compile order):"
            ),
            "",
        ]
        for lib, files in by_lib.items():
            lines.append(f"{lib}:")
            skipped = 0
            for p in files:
                if is_vunit_builtin(p):
                    skipped += 1
                    continue
                lines.append(f"  {p}")
            if skipped:
                lines.append(f"  (+ {skipped} VUnit built-in files omitted)")
        lines.append(
            f"Total: {project_files} project file(s) + {builtins} "
            "VUnit built-in file(s)."
        )
        return "\n".join(lines)
    except InternalProjectError as exc:
        return str(exc)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
