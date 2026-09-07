# vunit-mcp

<p align="center">
  <img src="logos/vunit-mcp.png" width="200" alt="VUnit badge with a red [MCP] stamp">
</p>

MCP (stdio) server that lets an LLM/agent drive a **VUnit** (HDL
unit-testing) project end to end: list tests, compile, run, and inspect
reports, per-test logs, and — for GHDL runs (and NVC on a VUnit with the
headless `--wave` flag) — record signal waveforms and hand the file path
off to a waveform-reading MCP server.

VUnit has no standalone CLI and `VUnit.main()` calls `sys.exit()`, so the
server never *runs* vunit in-process — it shells out to the project's own
`run.py`, exactly how a human runs it. **The server installs no VUnit at
all**: the only VUnit that ever answers a question is the project's own, so
the answers cannot disagree with what the project actually compiles. Even
`vunit_test_dependencies` ("which files do I need to implement this
test?"), which needs VUnit *internal* API with no CLI equivalent, runs as a
subprocess under the project's interpreter — see [Dependency
probe](#dependency-probe).

## Setup

```bash
uv venv .venv
uv pip install -e .            # installs vunit-mcp + mcp + pydantic — no vunit
```

VUnit itself belongs to the **project**, not here. Compile/run also need a
simulator (`ghdl`, `nvc`, …) on the `PATH` of the interpreter that runs
`run.py` — the project venv, which the server creates and activates for you
(see [Project virtualenv](#project-virtualenv)).

### Waveforms need `--wave` in the *project's* VUnit

Headless waveform recording (`waveform_format`) needs the `--wave` flag
from upstream [PR #1101](https://github.com/VUnit/vunit/pull/1101), which
no released VUnit has yet. Since the server ships no VUnit, whether
waveforms work is decided entirely by what the project installs:

| Project's VUnit | GHDL | NVC |
|---|---|---|
| has `--wave` (e.g. the [`ru551n/vunit`](https://github.com/ru551n/vunit) fork: 5.0.0.dev12 + PR #1101) | vcd, headless | fst, headless |
| stock (no `--wave`) | vcd/ghw via the legacy `--gtkwave-fmt` path | nothing recorded — the run says so |

`vunit_status` reports whether the flag is there, and the tool docs tell
the LLM to check it before promising a waveform.

On a VUnit **5.0** prerelease one project-side change applies: VUnit 5 no
longer compiles the HDL builtins by default, so a 4.x-style `run.py` must
add `PROJ.add_vhdl_builtins()` after `VUnit.from_argv()` (VUnit prints the
exact line to add if it is missing).

## Configuration (env vars)

| Variable | Meaning | Default |
|---|---|---|
| `VUNIT_MCP_PROJECT_DIR` | dir containing `run.py`/`simulate.py` | server's cwd |
| `VUNIT_MCP_RUN_SCRIPT` | run script path relative to project dir | `run.py`, else `simulate.py` |
| `VUNIT_MCP_PYTHON` | interpreter that runs `run.py` and the dependency probe (must have `vunit-hdl`); setting it disables venv auto-creation | the project venv's own python (see below) |
| `VUNIT_MCP_AUTO_VENV` | create a missing project venv with uv (`0`/`false`/`no`/`off` disables) | enabled |
| `VUNIT_MCP_UV` | `uv` executable used to create the venv | `uv` on `PATH` |
| `VUNIT_MCP_VENV_TIMEOUT` | max seconds for venv creation + dependency install | `900` |
| `VUNIT_MCP_SIMULATOR` | passed through as `VUNIT_SIMULATOR` | VUnit auto-detect |
| `VUNIT_MCP_OUTPUT_DIR` | default `-o` output path | `<project>/vunit_out` |
| `VUNIT_MCP_TIMEOUT` | max seconds per run/compile | `600` |
| `VUNIT_MCP_EXTRA_ARGS` | extra `run.py` args (escape hatch) | unset |
| `VUNIT_MCP_FINGERPRINT_EXCLUDE` | comma-separated patterns (fnmatch globs on file name or project-relative path, or a directory name) of registered files whose content changes must not invalidate the export cache — for generated/volatile files; adding or removing them still does | unset (fingerprint everything) |

### Project virtualenv

The project's own virtualenv is always used and **activated** for every
`run.py` subprocess — `VIRTUAL_ENV` set, `<venv>/bin` first on `PATH`,
`PYTHONHOME` cleared, and this server's own venv removed from the
environment — so nested `python`/`pip`/console-script lookups made by
`run.py` itself resolve inside it, not just the top-level interpreter.

Resolution order at startup:

1. `VUNIT_MCP_PYTHON`, if set (authoritative; when it points into a venv,
   that venv is activated too, and nothing is ever created).
2. An existing `<project>/.venv`, else `<project>/venv`.
3. Otherwise one is created with `uv`, from whichever of the project's
   dependency declarations works: `uv sync` for a `pyproject.toml`, else
   `uv venv` + `uv pip install -r requirements.txt`, else `uv venv` +
   `uv pip install -r pyproject.toml` (a pyproject that only carries tool
   config falls through to `requirements.txt` instead of failing the run).
4. If the project declares no dependencies, or `uv` is not installed, the
   old behavior applies: `python3`/`python` from `PATH` (this server's own
   venv excluded), and `vunit_status` reports why.

### Several agents on one code base

`vunit_run_tests` is serialized by an in-process lock, so one server per agent
removes the only interlock there is. Concurrent `run.py` invocations share
`<project>/vunit_out` (compiled libraries, `test_output/`, `junit.xml`) and will
clobber each other. Either give each agent its own `VUNIT_MCP_OUTPUT_DIR`, or —
simpler and fully disjoint — give each agent its own **git worktree** and start
the server with that worktree as cwd; output dir, venv, export cache and git
index are then separate with no configuration. Venv provisioning is safe
either way: discovery *and* creation happen under a cross-process lock keyed on
the project path (shared with tsfpga-mcp, which provisions the same venv), so a
server that arrives mid-install waits for the real thing instead of adopting a
virtualenv that has an interpreter but not yet any packages.

## MCP client config (Claude Code)

The server has runtime dependencies (mcp, pydantic), so run it with
`uvx` rather than a raw venv binary — it resolves and installs them into an
isolated environment for you:

```json
{
  "mcpServers": {
    "vunit": {
      "command": "uvx",
      "args": ["--from", "/path/to/vunit-mcp", "vunit-mcp"],
      "env": {
        "VUNIT_MCP_PROJECT_DIR": "/path/to/your/vunit/project"
      }
    }
  }
}
```

`--from` accepts a local checkout path or a git URL
(`--from "vunit-mcp @ git+https://github.com/<owner>/vunit-mcp.git"`).
A local checkout is installed by content hash, so edits to the server are
picked up automatically; `uvx --refresh` forces a re-resolve.

`VUNIT_MCP_PROJECT_DIR` is optional — it defaults to the server's current
working directory — but most MCP hosts launch servers from an arbitrary
directory, so set it explicitly unless you know the host's cwd is the
project.

Or with MCP Inspector for manual testing:

```bash
VUNIT_MCP_PROJECT_DIR=/path/to/project npx @modelcontextprotocol/inspector \
  uvx --from /path/to/vunit-mcp vunit-mcp
```

## Skill

This repo ships an agent skill, `skills/vunit-mcp/SKILL.md`, that tells the
LLM *when* and *how* to use the tools: which tool answers which request,
workflow recipes ("why did test X fail?" → `vunit_get_test_log`), the
`lib.entity[.test_case]` test-name format, and the `VUNIT_MCP_*` configuration.
Install it next to the server so the agent picks it up automatically.

### Claude Code

Symlinking keeps the repo checkout as the single source of truth (copy with
`cp -r` if you prefer a static install):

```bash
# personal — available in every project
ln -s /path/to/vunit-mcp/skills/vunit-mcp ~/.claude/skills/vunit-mcp

# or project-local — available only in that project
mkdir -p <your-project>/.claude/skills
ln -s /path/to/vunit-mcp/skills/vunit-mcp <your-project>/.claude/skills/vunit-mcp
```

### Maki

Maki loads skills from the same `~/.claude/skills/` directory:

```bash
ln -s /path/to/vunit-mcp/skills/vunit-mcp ~/.claude/skills/vunit-mcp
```

## Tools

| Tool | Needs sim | Description |
|---|---|---|
| `vunit_status` | no | config, vunit version, simulator availability — call first |
| `vunit_list_tests` | no | all tests (`lib.entity[.test_case]`) via `--list` |
| `vunit_list_files` | no | source files in compile order via `--files` |
| `vunit_compile` | yes | compile all sources (`--compile`) |
| `vunit_elaborate` | yes | elaborate test benches without running (`--elaborate`) |
| `vunit_run_tests` | yes | run tests (patterns, threads, clean, …); writes JUnit XML; returns pass/fail summary + failing tests. `waveform_format` (`"vcd"`, `"ghw"`, `"fst"`) records one waveform per test for `vunit_get_test_waveform`. The server records a canonical format per simulator — vcd on GHDL, fst on NVC — and normalizes other choices to it. vcd/ghw work on GHDL with any VUnit; a VUnit with the new `--wave` flag (upstream PR #1101) records headless for GHDL **and** NVC |
| `vunit_get_report` | no | answers *which* tests passed/failed — re-reads the last run's JUnit XML, no re-run, safe to call repeatedly; per-test status + failing-check counts; use it to pick a test before reading its log. `only_failing=true` hides passing tests from the per-test listing (the summary line still counts every test) — useful for large suites. `slowest=N` appends the N slowest tests by wall time |
| `vunit_get_test_log` | no | answers *why* one test failed — the single test's `output.txt`; last 100 lines by default (`lines` to raise), plus a parsed "Check results" section when the log contains failing-check lines |
| `vunit_get_test_waveform` | no | resolves the test's recorded waveform file (requires `waveform_format` at run time) and returns its **path** plus the failing check's sim time — hand VCD/FST paths to a waveform-reading MCP server; for GHW, either re-run with `waveform_format="vcd"`/`"fst"` for MCP-based analysis, or tell the human user to open the file in the gtkwave GUI themselves. No parsing, no re-simulation |
| `vunit_test_dependencies` | no | ordered list of source files needed to implement one test (grouped by library, compile order, VUnit built-ins summarized); caches a project model in `<project>/.vunit-mcp-cache` |
| `vunit_export_json` | no | project files, tests, and attributes via `--export-json`; cached in `<project>/.vunit-mcp-cache/export.json`, re-run only when the project's sources change |

## Export cache

`vunit_export_json` and `vunit_test_dependencies` do not re-run
`run.py --export-json` on every call: the exported model is written to
`<project>/.vunit-mcp-cache/export.json` together with a fingerprint of its
inputs, and served from that file while the fingerprint matches. The cache
invalidates when:

- any registered source file's mtime or size changes, or the file
  disappears;
- `run.py` itself changes (covers adding/removing/relocating files);
- `VUNIT_MCP_PYTHON`, `VUNIT_MCP_SIMULATOR`, or `VUNIT_MCP_EXTRA_ARGS`
  change.

Files matching `VUNIT_MCP_FINGERPRINT_EXCLUDE` (comma-separated fnmatch
globs on file name or project-relative path, or a directory name) are
exempt from the first rule — their mtime/size are not tracked, for
generated or volatile files whose rewrites would churn the cache. Their
name and existence are still tracked, so adding or removing one
invalidates as usual.

To force a fresh export, delete `.vunit-mcp-cache/export.json`. Dependency
answers are cached additionally, in memory, keyed by export content and
test name.

## Dependency probe

Some VUnit questions cannot be answered through the project's own `run.py`
CLI — e.g. "which files do I need to implement this test?", which needs
VUnit's internal `get_implementation_subset`. Importing VUnit in the server
would answer it with the *wrong* VUnit, so instead `dependency_probe.py` is
executed as a script by the **project's** interpreter:

```
<project venv python> dependency_probe.py     # request JSON on stdin
```

It is deliberately self-contained — stdlib + `vunit` only, never importing
`vunit_mcp`, since the project venv has vunit-mcp installed nowhere. It
rebuilds a `VUnit` instance from the cached `--export-json` model
(libraries and source files registered), calls the internal API, and writes
its reply to `<scratch>/result.json` rather than stdout, because VUnit logs
to stdout/stderr with no contract that it stays quiet.

That instance is **never** run through the CLI: the export model lacks the
user's `run.py` specifics (custom options, test attributes, requirements,
…), so anything that compiles or runs must go through the project's own
`run.py`.

The scratch dir is `<project>/.vunit-mcp-cache/model/<sha256 of export>` —
never the project's `vunit_out` (VUnit would wipe it) and never the
`.vunit-mcp-cache` root (which holds `export.json`). VUnit leaves a pickled
`project_database` there and reloads it next time, which makes it a parse
cache: parsing every source file is the expensive part. That path is
predictable, so a database planted there by a hostile project would be code
execution on `pickle.loads`; the first probe of each key therefore **wipes
any database it did not write itself**, and only later probes in the same
server process reuse one.

## Log-size policy

Tool output is deliberately bounded so it stays LLM-friendly — raw logs are
never dumped in full:

- `vunit_get_test_log` returns the **last 100 lines** by default and says so
  (e.g. "showing last 100 of 3421 lines"); raise `lines` for more. Even an
  explicit "full" read is capped at ~24 KB (the tail of the file).
- `vunit_compile` returns a 10-line tail on success and an **error-line
  excerpt** (error/fatal/failure lines + 2 lines of context) on failure.
  `vunit_elaborate` behaves the same way, but performs a real GHDL
  elaboration pass (`ghdl -e`), not just per-file analysis (`ghdl -a`) —
  it catches cross-unit errors (port/generic/type mismatches between an
  entity and its instantiations) that `vunit_compile` misses whenever the
  mismatched entity isn't exercised by a currently-selected test.
- All other raw-output fallbacks (failed `run.py`, unparseable output) are
  tail-truncated to 4 000 chars, keeping the end where errors and result
  lines live.
- `vunit_run_tests` / `vunit_get_report` return the parsed JUnit summary
  (counts + failing test names) rather than raw output.
- `vunit_export_json` inlines the JSON only below 8 000 chars; above that it
  returns counts + file/test name lists.
- Waveforms are never read or dumped by this server: `vunit_get_test_waveform`
  returns the recorded file's path (plus the failing check's sim time), and
  the actual waveform analysis happens in a separate waveform-reading MCP
  server that receives that path.
- `vunit_list_files` / `vunit_export_json` list project files only; VUnit
  built-in library sources (installed package files) are summarized as a
  count, since they are stable and not part of the project.

## Development

```bash
uv pip install -e ".[dev]"
uv run pytest tests/          # pure parsers — no simulator required
uv run ruff check src/ tests/
uv run mypy src/vunit_mcp/
```

A few tests exercise `dependency_probe.py` for real and need a VUnit; they
skip unless one is installed. To run them, sync the non-default `e2e`
dependency group — the *only* place vunit-hdl appears in this repo:

```bash
uv sync --group e2e
uv run pytest tests/
```
