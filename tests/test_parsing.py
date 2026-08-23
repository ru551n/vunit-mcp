"""Unit tests for the pure parsing helpers (no simulator needed)."""

from pathlib import Path

from vunit_mcp.parsing import (
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

JUNIT = """<?xml version="1.0" ?>
<testsuites>
  <testsuite name="vunit" tests="3" failures="1" errors="0" skipped="1" time="1.5">
    <testcase name="add" classname="tb_counter" time="0.4" />
    <testcase name="overflow" classname="tb_counter" time="0.5">
      <failure message="ASSERTION FAILED at time 100ns">log line</failure>
    </testcase>
    <testcase name="reset" classname="tb_counter" time="0.6">
      <skipped message="skipped by attribute" />
    </testcase>
  </testsuite>
</testsuites>
"""


def test_parse_test_list():
    out = "tb_counter.add\ntb_counter.overflow\nListed 2 tests\n"
    assert parse_test_list(out) == ["tb_counter.add", "tb_counter.overflow"]


def test_parse_test_list_empty():
    assert parse_test_list("Listed 0 tests\n") == []


def test_parse_file_list():
    out = "cntlib, counter.vhdl\nvunit_lib, /pkg/vunit/run.vhd\nListed 2 files\n"
    assert parse_file_list(out) == [
        "cntlib, counter.vhdl",
        "vunit_lib, /pkg/vunit/run.vhd",
    ]


def test_parse_junit(tmp_path: Path):
    p = tmp_path / "junit.xml"
    p.write_text(JUNIT)
    rep = parse_junit(p)
    assert isinstance(rep, JUnitReport)
    assert len(rep.tests) == 3
    assert rep.passed == 1
    assert rep.failures == 1
    assert rep.skipped == 1
    assert rep.time == 1.5
    failing = rep.failed
    assert len(failing) == 1
    assert failing[0].status == "failed"
    assert "ASSERTION FAILED" in failing[0].message
    names = {t.fullname for t in rep.tests}
    assert names == {
        "tb_counter.add",
        "tb_counter.overflow",
        "tb_counter.reset",
    }
    summary = rep.summary()
    assert "passed: 1" in summary
    assert "tb_counter.overflow" in summary


def test_parse_junit_suite_time_fallback(tmp_path: Path):
    """VUnit's JUnit omits the suite time; fall back to testcase sum."""
    p = tmp_path / "junit.xml"
    p.write_text(
        '<testsuite tests="2" failures="0" errors="0" skipped="0">'
        '<testcase name="a" classname="tb" time="0.25"/>'
        '<testcase name="b" classname="tb" time="0.75"/>'
        "</testsuite>"
    )
    rep = parse_junit(p)
    assert rep.time == 1.0


def test_mapping_and_log(tmp_path: Path):
    (tmp_path / "hash_abc").mkdir()
    (tmp_path / "hash_abc" / "output.txt").write_text("line1\nline2\nline3\n")
    (tmp_path / "test_name_to_path_mapping.txt").write_text(
        "hash_abc tb_counter.add\n"
    )
    mapping = parse_mapping_file(tmp_path)
    assert mapping == {"tb_counter.add": tmp_path / "hash_abc"}
    log = resolve_test_log(tmp_path, "tb_counter.add")
    assert log == tmp_path / "hash_abc" / "output.txt"
    assert resolve_test_log(tmp_path, "tb_counter.nope") is None


def test_mapping_nested_in_test_output(tmp_path: Path):
    # VUnit 4.x nests per-test output (and the mapping file) under test_output/
    test_output = tmp_path / "test_output"
    (test_output / "hash_abc").mkdir(parents=True)
    (test_output / "hash_abc" / "output.txt").write_text("boom\n")
    (test_output / "test_name_to_path_mapping.txt").write_text(
        "hash_abc tb_counter.add\n"
    )
    log = resolve_test_log(tmp_path, "tb_counter.add")
    assert log == test_output / "hash_abc" / "output.txt"


def test_mapping_file_missing(tmp_path: Path):
    assert parse_mapping_file(tmp_path) == {}


def test_read_tail(tmp_path: Path):
    p = tmp_path / "log.txt"
    p.write_text("a\nb\nc\n")
    assert read_tail(p) == "a\nb\nc\n"
    assert read_tail(p, lines=2) == "b\nc"


def test_is_vunit_builtin(monkeypatch):
    from vunit_mcp import parsing

    monkeypatch.setattr(parsing, "_VUNIT_PKG_DIR", "/opt/site-packages/vunit")
    monkeypatch.setattr(parsing, "_VUNIT_PKG_DIR_RESOLVED", True)
    assert is_vunit_builtin("/opt/site-packages/vunit/vhdl/run/src/run.vhd")
    assert not is_vunit_builtin("/home/me/proj/tb_counter.vhd")
    assert not is_vunit_builtin("/home/me/vunit_stuff/tb.vhd")
    # A project subdirectory named vunit/ is not a built-in.
    assert not is_vunit_builtin("/home/me/proj/vunit/tb.vhd")
    # A sibling package sharing the name prefix is not a built-in.
    assert not is_vunit_builtin("/opt/site-packages/vunitish/hdl/check.vhd")

    # Windows-style paths (backslashes on both sides).
    monkeypatch.setattr(parsing, "_VUNIT_PKG_DIR", "C:\\site-packages\\vunit")
    assert is_vunit_builtin("C:\\site-packages\\vunit\\hdl\\check\\check.vhd")

    # Fallback heuristic when the package cannot be located.
    monkeypatch.setattr(parsing, "_VUNIT_PKG_DIR", None)
    assert is_vunit_builtin("/opt/lib/vunit/vhdl/run/src/run.vhd")
    assert not is_vunit_builtin("/home/me/proj/tb_counter.vhd")


def test_find_simulator_error():
    assert find_simulator_error("", "No available simulator detected.") == (
        "No available simulator detected."
    )
    assert find_simulator_error("fine\n", "") is None


def test_read_tail_byte_cap(tmp_path: Path):
    p = tmp_path / "big.log"
    p.write_text("x" * 1000 + "\nEND\n")
    text = read_tail(p, max_bytes=16)
    assert text.endswith("END\n")
    assert len(text.encode()) <= 16


def test_count_lines(tmp_path: Path):
    p = tmp_path / "log.txt"
    p.write_text("a\nb\nc\n")
    assert count_lines(p) == (3, True)


def test_count_lines_capped(tmp_path: Path):
    p = tmp_path / "log.txt"
    p.write_text("x\n" * 10)
    n, exact = count_lines(p, max_bytes=4)
    assert exact is False
    assert 1 <= n <= 10


def test_error_excerpt_pulls_errors_with_context():
    text = (
        "compiling a.vhd\n"
        "compiling b.vhd\n"
        "b.vhd:10: error: syntax error\n"
        "    near 'if'\n"
        "compiling c.vhd\n"
        "c.vhd:5: fatal: cannot open file\n"
    )
    excerpt = error_excerpt(text)
    assert "error: syntax error" in excerpt
    assert "near 'if'" in excerpt  # context line
    assert "fatal: cannot open file" in excerpt
    assert "compiling a.vhd" not in excerpt  # noise dropped


def test_error_excerpt_falls_back_to_tail():
    text = "\n".join(f"line {i}" for i in range(100))
    excerpt = error_excerpt(text)
    assert "line 99" in excerpt
    assert len(excerpt.splitlines()) < 100


def test_error_excerpt_limits_hits():
    text = "\n".join(f"file{i}.vhd:1: error: boom" for i in range(50))
    excerpt = error_excerpt(text, max_hits=5)
    # hit cap + context: at most max_hits * 3 output lines
    assert len(excerpt.splitlines()) <= 5 * 3


def test_strip_ansi_removes_color_codes():
    from vunit_mcp.parsing import strip_ansi

    # Real NVC compile-error output shape (CSI SGR sequences).
    raw = "\x1b[31m** Error:\x1b[0m unexpected \x1b[33mend\x1b[0m while parsing"
    assert strip_ansi(raw) == "** Error: unexpected end while parsing"
    # OSC, two-byte sequences, and plain text untouched.
    assert strip_ansi("\x1b]0;title\x07text") == "text"
    assert strip_ansi("\x1b=alt\x1b>main") == "altmain"
    assert strip_ansi("no escapes here") == "no escapes here"
