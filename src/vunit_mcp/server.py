"""FastMCP server exposing a VUnit project as MCP tools.

The server shells out to the project's run.py (VUnit has no standalone CLI,
and VUnit.main() calls sys.exit(), so the server never *runs* vunit
in-process; the deliberate exception is vunit_test_dependencies, which
builds an in-process project model — see project_model).
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from .checks import count_passed_checks, parse_check_results, render_check_summary
from .config import Config, ConfigError, effective_simulator, load_config
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
    find_anchor_from_log,
    find_waveform_file,
    format_seconds,
    help_supports_wave_flag,
    run_waveform_args,
    waveform_unavailable_reason,
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

# Output dir of the most recent vunit_run_tests. The report/log/waveform
# tools read from here so a per-run output_dir override is honored (falls
# back to the configured default until the first run).
_last_output_dir: Path | None = None


def get_config() -> Config:
    global _config
    if _config is None:
        _config = load_config()
    return _config


def _effective_output_dir(config: Config) -> Path:
    return _last_output_dir if _last_output_dir is not None else config.output_dir


# Whether the project's VUnit has the new --wave flag (upstream PR #1101:
# headless waveform generation for GHDL and NVC). Probed once from
# run.py --help and cached for the server's lifetime (the config, and with
# it the VUnit install, is fixed per server).
_wave_flag_supported: bool | None = None


async def supports_wave_flag(config: Config) -> bool | None:
    """Whether this VUnit advertises the new --wave flag (upstream PR #1101).

    Probes ``run.py --help`` once and caches the result. Returns True/False
    on a successful probe; None when the probe itself failed (callers should
    treat that as "assume legacy" for runs, and report it for status). A
    failed probe is not cached, so a later call retries.
    """
    global _wave_flag_supported
    if _wave_flag_supported is None:
        err, out = await _probe(config, ["--help"])
        if err is None:
            _wave_flag_supported = help_supports_wave_flag(out or "")
    return _wave_flag_supported


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
    VUnit version, whether a simulator appears available, and which
    waveform-recording flags the VUnit install supports. Call this first
    when diagnosing setup problems."""
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

    supported = await supports_wave_flag(config)
    if supported is None:
        wave_note = "waveform probe failed (could not read run.py --help)"
    elif supported:
        wave_note = (
            "new --wave flag: headless waveforms for GHDL and NVC (vcd/fst/ghw)"
        )
    else:
        sim = effective_simulator(config)
        if sim and sim.strip().lower() == "nvc":
            wave_note = (
                "no headless waveforms: NVC on this VUnit needs the --wave "
                "release (or --gui); use GHDL to record waveforms"
            )
        else:
            wave_note = (
                "legacy --gtkwave-fmt: GHDL only, headless; NVC needs a newer VUnit"
            )

    sims = [s for s in SIMULATORS if shutil.which(s)]
    if config.simulator:
        sims_note = f"VUNIT_MCP_SIMULATOR={config.simulator} (passthrough)"
    elif os.environ.get("VUNIT_SIMULATOR"):
        # Effective simulator via VUnit's own env var (not overridden by us).
        sims_note = f"VUNIT_SIMULATOR={os.environ['VUNIT_SIMULATOR']}"
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
            f"- waveform    : {wave_note}",
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
    # Waveform args are added by the caller: they depend on the one-time
    # --wave capability probe (see supports_wave_flag).
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
    args += input.test_patterns
    return args


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, openWorldHint=False))
async def vunit_run_tests(
    input: RunTestsInput = RunTestsInput(),  # noqa: B008 (FastMCP pattern)
) -> str:
    """Run VUnit tests and return a pass/fail summary plus the list of
    failing tests. Patterns default to ['*'] (run everything). A JUnit XML
    is always written next to the output dir for vunit_get_report.
    Requires a simulator. Set waveform_format to record one waveform per
    test: 'vcd'/'ghw' work on GHDL with any VUnit; a VUnit with the --wave
    flag (upstream PR #1101) records headless for GHDL and NVC and unlocks
    'fst' (NVC's default compact format). With NVC set
    (VUNIT_SIMULATOR/VUNIT_MCP_SIMULATOR) on an older
    VUnit the tests still run but no waveform is recorded (it says so in the
    result). vunit_get_test_waveform then returns the file path so a
    waveform MCP server can inspect signal behavior."""
    try:
        config = get_config()
    except ConfigError as exc:
        return _err(exc)
    global _last_output_dir
    if input.output_dir:
        output_dir = Path(input.output_dir).expanduser()
        if not output_dir.is_absolute():
            # Relative against the project dir, not the server's cwd (which
            # is wherever the MCP host launched us).
            output_dir = config.project_dir / output_dir
        output_dir = output_dir.resolve()
    else:
        output_dir = config.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    _last_output_dir = output_dir
    args = ["-o", str(output_dir), *_run_args(input, output_dir)]
    wave_note = None
    if input.waveform_format:
        # A failed probe yields None; treat that as "assume legacy" so a
        # flaky --help probe never blocks an otherwise-valid run.
        wave_flag = bool(await supports_wave_flag(config))
        reason = waveform_unavailable_reason(
            effective_simulator(config), wave_flag
        )
        if reason is not None:
            # e.g. NVC on a legacy VUnit: the run is still valid, but no
            # waveform will be recorded. Don't pass the (ignored) flag.
            wave_note = f"Waveform not recorded ({input.waveform_format}): {reason}"
        else:
            try:
                args += run_waveform_args(input.waveform_format, wave_flag)
            except ValueError as exc:
                return f"Waveform recording not available: {exc}"
    start = time.time()
    try:
        result = await run_vunit(config, args, timeout=input.timeout)
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
            if wave_note is not None:
                out += f"\n{wave_note}"
            elif input.waveform_format and report.failed:
                out += (
                    f"\nWaveforms recorded ({input.waveform_format.upper()}) "
                    "— for a failing test, call vunit_get_test_waveform"
                    "(test_name) to get the waveform file path for a "
                    "waveform MCP server."
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
    output_dir = _effective_output_dir(config)
    report_path = await resolve_junit_path(output_dir)
    if not report_path or not report_path.is_file():
        return f"No JUnit report found in {output_dir}. Run vunit_run_tests first."
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
    output_dir = _effective_output_dir(config)
    for t in report.tests:
        line = f"- [{t.status.upper()}] {t.fullname} ({t.time:.3f}s)"
        # Only failing tests can have failing checks; skip the log read
        # entirely for everything else.
        if t.status in ("failed", "error"):
            n = _failing_checks(output_dir, t.fullname)
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
    output_dir = _effective_output_dir(config)
    log_path = resolve_test_log(output_dir, input.test_name)
    if log_path is None:
        known = sorted(parse_mapping_file(output_dir))
        hint = ""
        if known:
            hint = (
                "\nKnown tests (last run):\n" + "\n".join(f"- {n}" for n in known)
            )
        return f"No log found for test {input.test_name!r} in {output_dir}.{hint}"
    shown = input.lines or 0
    text = read_tail(log_path, shown if shown else None)
    total, exact = count_lines(log_path)
    shown_lines = shown or len(text.splitlines())
    header = f"Log for {input.test_name} ({log_path})"
    if total > shown_lines:
        header += (
            f" — showing last {shown_lines} of {total}"
            f"{'+' if not exact else ''} lines; raise `lines` for more"
        )
    out = f"{header}:\n---\n{text}"
    summary = render_check_summary(
        parse_check_results(text), count_passed_checks(text)
    )
    if summary:
        out += f"\n\n{summary}\n(line numbers refer to the log shown above)"
    return out


_WAVEFORM_USE = {
    ".vcd": (
        "Hand this path to a waveform-reading MCP server (e.g. waveform-mcp) "
        "to read signal values, search signal names, or zoom in around the "
        "failing time — do not dump the raw VCD into the conversation."
    ),
    ".fst": (
        "Hand this path to a waveform-reading MCP server (e.g. waveform-mcp) "
        "to read signal values, search signal names, or zoom in around the "
        "failing time — do not dump the raw FST into the conversation. FST "
        "is NVC's default, compact machine-readable format (GTKWave can "
        "open it too)."
    ),
    ".ghw": (
        "GHW is for opening in the gtkwave GUI. For MCP-based waveform "
        'analysis, re-run the test with waveform_format="vcd" and call this '
        "tool again."
    ),
}


def _human_size(num: int) -> str:
    value = float(num)
    unit = "B"
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            break
        value /= 1024
    return f"{value:,.0f} {unit}" if unit == "B" else f"{value:,.1f} {unit}"


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
async def vunit_get_test_waveform(input: GetTestWaveformInput) -> str:
    """Resolves the waveform file recorded for a test by vunit_run_tests
    (waveform_format 'vcd', 'ghw', or 'fst') and returns its path — hand a
    VCD/FST path to a waveform-reading MCP server, or open a GHW file in the
    gtkwave GUI. Also reports the failing check's simulation time from the
    test log when present, so you know where to look. No re-simulation, no
    waveform parsing."""
    try:
        config = get_config()
    except ConfigError as exc:
        return _err(exc)

    output_dir = _effective_output_dir(config)
    mapping = parse_mapping_file(output_dir)
    test_dir = mapping.get(input.test_name)
    if test_dir is None:
        known = sorted(mapping)
        hint = ""
        if known:
            hint = "\nKnown tests (last run):\n" + "\n".join(f"- {n}" for n in known)
        return f"No data for test {input.test_name!r} in {output_dir}.{hint}"

    wave = find_waveform_file(test_dir, input.waveform_format)
    if wave is None:
        return (
            f"No waveform recorded for {input.test_name}. Run vunit_run_tests "
            'with waveform_format="vcd" (GHDL) or "fst" (NVC default) to '
            "record one, then call this tool again."
        )

    try:
        size = _human_size(wave.stat().st_size)
    except OSError:
        return f"Waveform file disappeared while being reported: {wave}"
    lines = [
        f"Waveform for {input.test_name}:",
        f"Path: {wave}",
        f"Format: {wave.suffix.lstrip('.').upper()}",
        f"Size: {size}",
    ]
    log_path = test_dir / "output.txt"
    if log_path.is_file():
        secs, msg = find_anchor_from_log(read_tail(log_path))
        if secs is not None:
            lines.append(f'Failing check at {format_seconds(secs)}: "{msg[:120]}"')
    lines += ["", _WAVEFORM_USE.get(wave.suffix, "")]
    return "\n".join(lines).rstrip()


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
