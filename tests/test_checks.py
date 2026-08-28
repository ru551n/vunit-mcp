"""Tests for the check-result log parser (checks.py)."""

from vunit_mcp.checks import (
    CheckHit,
    count_passed_checks,
    parse_check_results,
    render_check_summary,
)

# Real sample from a VUnit 4.7.1 GHDL run (padded process/severity columns).
GHDL_ERROR = "       200000000 fs - check                -   ERROR - this check is deliberately wrong"  # noqa: E501  # real sample: alignment is significant


def test_ghdl_real_sample_line():
    hits = parse_check_results(GHDL_ERROR)
    assert hits == [
        CheckHit(
            severity="ERROR",
            message="this check is deliberately wrong",
            line_no=1,
            time_str="200000000 fs",
        )
    ]


def test_ghdl_multiple_lines_and_severities():
    log = (
        "       0 ns - check - INFO - test started\n"
        + GHDL_ERROR
        + "\n"
        + "       300000000 ns - check - WARNING - something odd but ok\n"
        + "       400000000 ns - check - FAILURE - second problem"
    )
    hits = parse_check_results(log)
    assert [(h.severity, h.line_no, h.message) for h in hits] == [
        ("ERROR", 2, "this check is deliberately wrong"),
        ("WARNING", 3, "something odd but ok"),
        ("FAILURE", 4, "second problem"),
    ]
    # INFO lines (incl. a pass line) are not hits.
    assert all(h.severity not in ("INFO", "NOTE") for h in hits)


def test_modelsim_lines():
    log = (
        "** ERROR: this check is deliberately wrong (line 42)\n"
        "** WARNING: queue nearly full\n"
        "# ** ERROR: another one"
    )
    hits = parse_check_results(log)
    assert [(h.severity, h.message) for h in hits] == [
        ("ERROR", "this check is deliberately wrong (line 42)"),
        ("WARNING", "queue nearly full"),
        ("ERROR", "another one"),
    ]


def test_nvc_style_lines():
    log = (
        "200000000fs ERROR: this check is deliberately wrong\n"
        "1000 ns ERROR: spaced time unit\n"
        "5 ns FAILURE: nvc failure line"
    )
    hits = parse_check_results(log)
    assert [(h.severity, h.time_str, h.message) for h in hits] == [
        ("ERROR", "200000000fs", "this check is deliberately wrong"),
        ("ERROR", "1000 ns", "spaced time unit"),
        ("FAILURE", "5 ns", "nvc failure line"),
    ]


def test_noise_lines_do_not_match():
    noise = (
        "ghdl:error: can't find elaboration unit called 'tb_counter'\n"
        "simulation failed\n"
        "Test case cntlib_tb.tb_counter.deliberate_fail failed after 0.005 s:\n"
        "This check is deliberately wrong (line 42)\n"
        "ERROR: no leading timestamp\n"
        "Compiling counter.vhdl\n"
        "ERROR: bad -- this is not a report line either (no time prefix)"
    )
    assert parse_check_results(noise) == []
    assert count_passed_checks(noise) == 0


def test_pass_visible_lines_counted_not_listed():
    log = (
        "       0 ns - check - INFO - Check passed (line 12)\n"
        "       100 ns - check - INFO - Check passed (line 15)\n" + GHDL_ERROR
    )
    hits = parse_check_results(log)
    assert [h.severity for h in hits] == ["ERROR"]
    assert count_passed_checks(log) == 2
    # No pass lines at all (the common case): 0.
    assert count_passed_checks(GHDL_ERROR) == 0


def test_empty_and_short_logs():
    assert parse_check_results("") == []
    assert parse_check_results("ok\nall good\n") == []
    assert count_passed_checks("") == 0


def test_render_check_summary_empty():
    assert render_check_summary([]) == ""
    assert render_check_summary([], passed=0) == ""


def test_render_check_summary_bounded():
    hits = [
        CheckHit("ERROR", f"failure number {i}", line_no=i, time_str=f"{i} ns")
        for i in range(1, 16)
    ]
    out = render_check_summary(hits, passed=7, max_shown=10)
    lines = out.splitlines()
    assert lines[0] == "Check results: 15 failing check(s), 7 visible passed check(s)"
    assert "- [ERROR] line 1 at 1 ns: failure number 1" in lines
    assert "  (+ 5 more — see full log)" in lines
    assert len(lines) == 1 + 10 + 1


def test_render_check_summary_warnings_only():
    hits = [CheckHit("WARNING", "meh", line_no=3)]
    out = render_check_summary(hits)
    assert out.splitlines()[0] == "Check results: 0 failing check(s), 1 warning(s)"
