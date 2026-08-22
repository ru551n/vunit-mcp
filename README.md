# vunit-mcp

MCP (stdio) server that lets an LLM/agent drive a **VUnit** (HDL
unit-testing) project end to end: list tests, compile, run, and inspect
reports and per-test logs.

VUnit has no standalone CLI and `VUnit.main()` calls `sys.exit()`, so the
server never *runs* vunit in-process — it shells out to the project's own
`run.py`, exactly how a human runs it. One deliberate exception:
`vunit_test_dependencies` builds an in-process project model to answer
"which files do I need to implement this test?". vunit-hdl is a hard
dependency of this package, so the import is always available; it is still
imported lazily, only when that tool is called.

## Logo

Candidate logos, all based on the official VUnit badge (blue `#0c479d`,
white ring, chunky V). SVG sources live in `logos/`; PNGs are 400×400
previews.

| **stamp** — tilted MCP rubber stamp | **chip** — the V cradles an AI chip |
|:--:|:--:|
| <img src="logos/vunit-mcp-stamp.png" width="180"> | <img src="logos/vunit-mcp-chip.png" width="180"> |
| **robot** — a robot buddy in the corner | **wordmark** — V with MCP type below |
|:--:|:--:|
| <img src="logos/vunit-mcp-robot.png" width="180"> | <img src="logos/vunit-mcp-wordmark.png" width="180"> |

## Setup

```bash
uv venv .venv
uv pip install -e .            # installs vunit-mcp + mcp + pydantic + vunit-hdl
# compile/run also need a simulator, in the env that runs run.py
# (default: this same venv):
uv pip install ghdl
```

## Configuration (env vars)

| Variable | Meaning | Default |
|---|---|---|
| `VUNIT_MCP_PROJECT_DIR` | dir containing `run.py` (required by all tools) | — |
| `VUNIT_MCP_RUN_SCRIPT` | run script path relative to project dir | `run.py` |
| `VUNIT_MCP_PYTHON` | interpreter that runs `run.py` (must have `vunit-hdl` + a simulator; the default has both) | server's own |
| `VUNIT_MCP_SIMULATOR` | passed through as `VUNIT_SIMULATOR` | VUnit auto-detect |
| `VUNIT_MCP_OUTPUT_DIR` | default `-o` output path | `<project>/vunit_out` |
| `VUNIT_MCP_TIMEOUT` | max seconds per run/compile | `600` |
| `VUNIT_MCP_EXTRA_ARGS` | extra `run.py` args (escape hatch) | unset |
| `VUNIT_MCP_FINGERPRINT_EXCLUDE` | comma-separated patterns (fnmatch globs on file name or project-relative path, or a directory name) of registered files whose content changes must not invalidate the export cache — for generated/volatile files; adding or removing them still does | unset (fingerprint everything) |

## MCP client config (Claude Code)

```json
{
  "mcpServers": {
    "vunit": {
      "command": "/home/sebbe/git/vunit-mcp/.venv/bin/vunit-mcp",
      "env": {
        "VUNIT_MCP_PROJECT_DIR": "/path/to/your/vunit/project"
      }
    }
  }
}
```

Or with MCP Inspector for manual testing:

```bash
VUNIT_MCP_PROJECT_DIR=/path/to/project npx @modelcontextprotocol/inspector \
  /home/sebbe/git/vunit-mcp/.venv/bin/python -m vunit_mcp
```

## Skill

This repo ships an agent skill, `skills/vunit-mcp/SKILL.md`, that tells the
LLM *when* and *how* to use the tools: which tool answers which request,
workflow recipes ("why did test X fail?" → `vunit_get_test_log`), the
`lib.entity[.proc]` test-name format, and the `VUNIT_MCP_*` configuration.
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
| `vunit_list_tests` | no | all tests (`lib.entity[.proc]`) via `--list` |
| `vunit_list_files` | no | source files in compile order via `--files` |
| `vunit_compile` | yes | compile all sources (`--compile`) |
| `vunit_run_tests` | yes | run tests (patterns, threads, clean, …); writes JUnit XML; returns pass/fail summary + failing tests |
| `vunit_get_report` | no | re-read the last run's JUnit XML, no re-run; per-test failing-check counts derived from the logs |
| `vunit_get_test_log` | no | per-test `output.txt` — how to see *why* a test failed; last 100 lines by default (`lines` to raise), plus a parsed "Check results" section when the log contains failing-check lines |
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

To force a fresh export, delete `.vunit-mcp-cache/export.json`. The
in-process project model used by `vunit_test_dependencies` is cached
additionally, in memory, keyed by export content.

## Internal scaffold

Some VUnit questions cannot be answered through the project's own `run.py`
CLI — e.g. "which files do I need to implement this test?". For those,
vunit-mcp builds an **in-process VUnit project** ("the scaffold") from the
cached `--export-json` model: a real `VUnit` instance with the project's
libraries and source files registered, used only to call VUnit's *internal*
API (today `get_implementation_subset` via `vunit_test_dependencies`; more
internal queries will build on it).

The scaffold is **never** run through the CLI: the export model does not
contain all of the user's `run.py` specifics (custom options, test
attributes, requirements, …), so anything that compiles or runs must go
through the project's own `run.py`. The in-process instance lives in
`project_model.InternalProject`, is cached in memory per export content,
and uses `<project>/.vunit-mcp-cache` as its scratch dir (never the
project's `vunit_out`, which VUnit would wipe).

## Log-size policy

Tool output is deliberately bounded so it stays LLM-friendly — raw logs are
never dumped in full:

- `vunit_get_test_log` returns the **last 100 lines** by default and says so
  (e.g. "showing last 100 of 3421 lines"); raise `lines` for more. Even an
  explicit "full" read is capped at ~24 KB (the tail of the file).
- `vunit_compile` returns a 10-line tail on success and an **error-line
  excerpt** (error/fatal/failure lines + 2 lines of context) on failure.
- All other raw-output fallbacks (failed `run.py`, unparseable output) are
  tail-truncated to 4 000 chars, keeping the end where errors and result
  lines live.
- `vunit_run_tests` / `vunit_get_report` return the parsed JUnit summary
  (counts + failing test names) rather than raw output.
- `vunit_export_json` inlines the JSON only below 8 000 chars; above that it
  returns counts + file/test name lists.
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
