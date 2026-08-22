"""Pydantic v2 input models for the MCP tools."""

from __future__ import annotations

from pydantic import BaseModel, Field


class RunTestsInput(BaseModel):
    """Input for vunit_run_tests."""

    test_patterns: list[str] = Field(
        default=["*"],
        description=(
            "VUnit test patterns (lib.entity[.proc]). Default ['*'] runs "
            "everything. Supports VUnit wildcards."
        ),
    )
    num_threads: int | None = Field(
        default=None,
        ge=1,
        description="Number of parallel test threads (-p). Defaults to VUnit's own default.",
    )
    output_dir: str | None = Field(
        default=None,
        description="Output directory (-o). Defaults to VUNIT_MCP_OUTPUT_DIR.",
    )
    timeout: float | None = Field(
        default=None,
        gt=0,
        description="Max seconds for the run. Defaults to VUNIT_MCP_TIMEOUT.",
    )
    clean: bool = Field(default=False, description="Clean build before running (--clean).")
    verbose: bool = Field(
        default=False, description="Verbose simulator output (--verbose)."
    )
    fail_fast: bool = Field(
        default=False, description="Stop on first failure (--fail-fast)."
    )
    with_attributes: bool = Field(
        default=False, description="Run tests with attributes (--with-attributes)."
    )
    without_attributes: bool = Field(
        default=False,
        description="Skip tests requiring attributes (--without-attributes).",
    )


class GetTestLogInput(BaseModel):
    """Input for vunit_get_test_log."""

    test_name: str = Field(
        description="Full test name as listed by vunit_list_tests (lib.entity.proc)."
    )
    lines: int | None = Field(
        default=100,
        ge=1,
        description=(
            "Return only the last N lines of the log (default 100, since "
            "failure info appears at the end). Pass a larger value for more "
            "context; the response is always size-capped (~24 KB max)."
        ),
    )


class TestDependenciesInput(BaseModel):
    """Input for vunit_test_dependencies."""

    test_name: str = Field(
        description=(
            "Full test name as listed by vunit_list_tests "
            "(lib.entity.proc), or a wildcard pattern (e.g. "
            "lib.entity.*). An ambiguous pattern returns the list of "
            "matches instead."
        )
    )


class ScaffoldFile(BaseModel):
    """One source file to register in a generated VUnit run script."""

    path: str = Field(
        description="Source file path (absolute, or relative to target_dir)."
    )
    library: str = Field(
        default="work",
        description="VUnit library name to compile the file into (default 'work').",
    )


class ScaffoldInput(BaseModel):
    """Input for vunit_scaffold."""

    target_dir: str = Field(
        description="Directory to create the run script in (created if missing)."
    )
    files: list[ScaffoldFile] | None = Field(
        default=None,
        description=(
            "Source files to register. Mutually exclusive with export_json."
        ),
    )
    export_json: str | None = Field(
        default=None,
        description=(
            "Path to a --export-json file (e.g. .vunit-mcp-cache/export.json "
            "in the project, maintained by vunit_export_json). Project files "
            "and their libraries are read from it; VUnit built-ins are "
            "skipped. Mutually exclusive with files."
        ),
    )
    run_script: str = Field(
        default="run.py", description="Run script name to create in target_dir."
    )
    overwrite: bool = Field(
        default=False,
        description="Overwrite the run script if it already exists.",
    )
    copy_files: bool = Field(
        default=False,
        description=(
            "Copy source files into target_dir and register the copies, "
            "making the new directory self-contained. Use when building a "
            "new project directory from an export of another project."
        ),
    )
