# VUnit MCP

## Overview
Use this skill whenever the user asks to work with a **VUnit** (HDL unit-testing)
project through the `vunit-mcp` MCP server: running tests, checking why a test
failed, compiling, listing tests/files, or asking which files a test depends on.
The server drives the project's own `run.py`, so every tool operates on the
project configured by `VUNIT_MCP_PROJECT_DIR`.

Triggers on: run my VUnit tests, run the regression, VUnit test failed, why did
test X fail, show me the waveform, what were the signals doing when X failed,
compile my VUnit project, list VUnit tests, which files do I need
to implement this test, VUnit status, VUnit export json, VUnit traceability,
VUnit requirements, check VUnit project.

If a tool returns a configuration error or the behavior seems off, call
`vunit_status` first — it reports the project dir, run script, interpreter,
VUnit version, and simulator availability.

## Tools

| Tool | What it does | Needs simulator? |
| --- | --- | --- |
| `vunit_status` | Server config + VUnit version + simulator availability. Diagnose config problems here. | no |
| `vunit_list_tests` | All test cases as `lib.entity[.test_case]`. Use to get exact names and to check patterns before a run. | no |
| `vunit_list_files` | All source files in compile order. | no |
| `vunit_compile` | Compile all sources (`--compile`). Safe to re-run; incremental. | yes |
| `vunit_run_tests` | Run tests (default: everything). Returns pass/fail summary + failing test names; writes a JUnit XML. | yes |
| `vunit_get_report` | Answers **"which tests passed/failed?"** — re-reads the last run's JUnit XML (no re-run, no simulator, safe to call repeatedly). Per-test status + failing-check counts. Use it to pick a test before reading its log. | no |
| `vunit_get_test_log` | Answers **"why did this test fail?"** — the single test's raw output (last 100 lines by default; failure info is at the end). Failing checks get a structured summary. For run-wide questions use `vunit_get_report` instead. | no |
| `vunit_get_test_waveform` | Answers **"what were the signals doing when it failed?"** — reads the test's recorded VCD (requires the run to have used `waveform_format="vcd"`, GHDL only). Anchors on the failing check's sim time; per-signal transition trace + snapshot. No re-simulation. | no |
| `vunit_export_json` | Full project model (files, tests, attributes incl. requirements/traceability) as JSON. Cached; refreshed only when sources change. | no |
| `vunit_test_dependencies` | Ordered source files needed to implement one test, grouped by library in compile order. Answer to "which files do I need for this test?". | no |

## `vunit_run_tests` inputs
- `test_patterns` — VUnit patterns like `lib.entity[.test_case]`; default `['*']` runs everything.
- `num_threads` — parallel test threads (`-p`).
- `output_dir` — where logs/JUnit go (default `VUNIT_MCP_OUTPUT_DIR`, i.e. `<project>/vunit_out`).
- `timeout` — max seconds (default `VUNIT_MCP_TIMEOUT`, 600).
- `clean` — clean build first (`--clean`); use after odd compile-state errors.
- `verbose`, `fail_fast`, `with_attributes`, `without_attributes` — pass through to VUnit.
- `waveform_format` — `"vcd"` records one VCD per test (GHDL only; needed for
  `vunit_get_test_waveform`). Costs compile/sim time, so use it when you
  expect to inspect a failure, not on every green run.

## Workflows (user request → tool calls)

**"Run my tests / run the regression"**
→ `vunit_run_tests` (add patterns if they name a subset). Report the summary;
offer to dig into failures.

**"Why did test X fail?"**
→ `vunit_get_test_log(test_name="lib.entity.test1")`. If you don't have the
exact name yet: `vunit_get_report` to find failing tests, then get its log.
If the log is cut off, re-call with a larger `lines`.

**"Why did test X fail? (signal level)" / "show me the waveform"**
→ `vunit_get_test_waveform(test_name=...)` — anchors on the failing check's
time and shows per-signal transitions + a snapshot. The run must have used
`waveform_format="vcd"` (GHDL); if not, re-run that test with it and call
again. Focus with `signals=[...]` (name or suffix), `time="50 ns"` +
`window="100 ns"` to move the anchor/window, and `max_transitions` to cap
chatty signals (e.g. clocks — often worth excluding via `signals`).
A log whose message already tells the whole story (e.g. a check_equal diff)
may not need the waveform at all.

**"Check that the project still compiles"**
→ `vunit_compile` (fast, incremental; no simulator run of the tests).

**"Which tests are in the project?" / "Is there a test for X?"**
→ `vunit_list_tests`.

**"Which files do I need to implement / elaborate test X?"**
→ `vunit_test_dependencies(test_name=...)`. A wildcard that matches several
tests returns the match list — then call again with an exact name.

**"Show me the project structure / files in order"**
→ `vunit_list_files` (quick) or `vunit_export_json` (full model incl. attributes).

**"Traceability / requirements coverage"**
→ `vunit_export_json` — attributes carry requirement/traceability data.
Large exports return counts + names and point at the full JSON file on disk.

**Anything looks wrong (wrong project, no simulator, stale results)**
→ `vunit_status`, then fix the configuration (see below) and retry.

## Rules of thumb
- Test names are always the full `lib.entity[.test_case]` form from
  `vunit_list_tests`; patterns accept VUnit wildcards.
- If a run says *no tests matched the patterns*, call `vunit_list_tests` and
  retry with a valid pattern — do not guess names.
- Prefer `vunit_get_report` over re-running to inspect the last run's results;
  it is free (no simulation).
- `vunit_get_test_log` shows the **tail** of the log by default (failures are at
  the end); raise `lines` only when you need earlier context. Responses are
  size-capped (~24 KB).
- Waveforms are read from the recorded VCD — never re-simulate just to look
  at one; and never ask for the raw VCD file, `vunit_get_test_waveform` is the
  only way in.
- No simulator: the tool tells you. Install one (e.g. ghdl or nvc) or set
  `VUNIT_MCP_SIMULATOR` to a VUnit-supported name.
- After changing run.py or VUnit config, results may be stale — re-run the
  tool that matters; `vunit_compile`/`vunit_run_tests` always reflect current
  sources.

## Configuration (env vars at server start)
- `VUNIT_MCP_PROJECT_DIR` — required; directory containing `run.py`.
- `VUNIT_MCP_RUN_SCRIPT` — run script relative to project dir (default `run.py`).
- `VUNIT_MCP_PYTHON` — interpreter that runs `run.py` (must have `vunit-hdl` + a simulator).
- `VUNIT_MCP_SIMULATOR` — passed through as `VUNIT_SIMULATOR`.
- `VUNIT_MCP_OUTPUT_DIR` — default output dir (default `<project>/vunit_out`).
- `VUNIT_MCP_TIMEOUT` — max seconds per run/compile (default 600).
- `VUNIT_MCP_EXTRA_ARGS` — extra `run.py` args (escape hatch).
- `VUNIT_MCP_FINGERPRINT_EXCLUDE` — comma-separated fnmatch patterns (file
  name, project-relative path, or directory) of generated/volatile files whose
  content changes must not invalidate the export cache; adding/removing them
  still does.

The `--export-json` cache lives at `<project>/.vunit-mcp-cache/export.json` and
is re-run automatically when project sources change.
