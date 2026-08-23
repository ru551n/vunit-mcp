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
    waveform_format: Literal["vcd", "ghw"] | None = Field(
        default=None,
        description=(
            "Record waveforms during the run (--gtkwave-fmt, GHDL only). "
            "'vcd' is needed before vunit_get_test_waveform; 'ghw' is for "
            "opening in a gtkwave GUI."
        ),
    )


class GetTestWaveformInput(BaseModel):
    """Input for vunit_get_test_waveform."""

    test_name: str = Field(
        description="Full test name as listed by vunit_list_tests (lib.entity.test_case)."
    )
    time: str | None = Field(
        default=None,
        description=(
            "Anchor time, e.g. '50 ns' or '50000000 fs'. Default: the time of "
            "the first failing check in the test log."
        ),
    )
    window: str | None = Field(
        default=None,
        description=(
            "Half-width of the transition window around the anchor, e.g. "
            "'100 ns'. Default: 100 ns."
        ),
    )
    signals: list[str] = Field(
        default=[],
        description=(
            "Signal names or suffixes to show, e.g. ['count', 'inc']. "
            "Empty: all signals active within the window (bounded)."
        ),
    )
    max_transitions: int = Field(
        default=100,
        ge=1,
        description="Max transitions shown per signal (closest to the anchor win).",
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

