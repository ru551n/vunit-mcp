"""Pydantic v2 input models for the MCP tools."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class RunTestsInput(BaseModel):
    """Input for vunit_run_tests."""

    test_patterns: list[str] = Field(
        default=["*"],
        description=(
            "VUnit test patterns (lib.entity[.test_case]). Default ['*'] runs "
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
        description=(
            "Output directory (-o). Relative paths resolve against the "
            "project dir. Defaults to VUNIT_MCP_OUTPUT_DIR."
        ),
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
    with_attributes: list[str] = Field(
        default_factory=list,
        description=(
            "Only run tests with these attributes set (--with-attributes "
            "<name>, repeatable)."
        ),
    )
    without_attributes: list[str] = Field(
        default_factory=list,
        description=(
            "Only run tests without any of these attributes set "
            "(--without-attributes <name>, repeatable)."
        ),
    )
    waveform_format: Literal["vcd", "ghw", "fst"] | None = Field(
        default=None,
        description=(
            "Record waveforms during the run. The server records a "
            "canonical format per simulator — 'vcd' on GHDL, 'fst' on NVC "
            "(compact, machine-readable; best for external waveform MCPs) — "
            "and normalizes any other explicit choice to it, saying so in "
            "the result. Requires the new --wave flag on a VUnit for "
            "headless NVC recording."
        ),
    )


class GetTestWaveformInput(BaseModel):
    """Input for vunit_get_test_waveform."""

    test_name: str = Field(
        description="Full test name as listed by vunit_list_tests (lib.entity.test_case)."
    )
    waveform_format: Literal["vcd", "ghw", "fst"] | None = Field(
        default=None,
        description=(
            "Which recorded waveform to resolve ('vcd', 'fst', or 'ghw'). "
            "Default: VCD if recorded, then FST, then GHW."
        ),
    )


class GetTestLogInput(BaseModel):
    """Input for vunit_get_test_log."""

    test_name: str = Field(
        description="Full test name as listed by vunit_list_tests (lib.entity.test_case)."
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
            "(lib.entity.test_case), or a wildcard pattern (e.g. "
            "lib.entity.*). An ambiguous pattern returns the list of "
            "matches instead."
        )
    )

