"""Unit tests for the pure run-script generation helpers (no sim needed)."""

from vunit_mcp.scaffold import entries_from_export, languages, render_run_py


def test_languages_detects_mixed():
    entries = [
        ("cntlib", "a/counter.vhdl"),
        ("cntlib_tb", "a/tb.vhd"),
        ("vlib", "b/top.sv"),
    ]
    assert languages(entries) == {"vhdl", "verilog"}


def test_languages_empty():
    assert languages([]) == set()


def test_entries_from_export_skips_builtins_and_dedupes():
    data = {
        "files": [
            {"file_name": "/proj/counter.vhdl", "library_name": "cntlib"},
            {
                "file_name": (
                    "/usr/lib/python3/site-packages/vunit/vhdl/core/src/core_pkg.vhd"
                ),
                "library_name": "vunit_lib",
            },
            {"file_name": "/proj/counter.vhdl", "library_name": "cntlib"},
            {"file_name": "/proj/tb.vhd", "library_name": "cntlib_tb"},
        ],
        "tests": [],
    }
    assert entries_from_export(data) == [
        ("cntlib", "/proj/counter.vhdl"),
        ("cntlib_tb", "/proj/tb.vhd"),
    ]


def test_entries_from_export_empty():
    assert entries_from_export({"files": []}) == []
    assert entries_from_export({}) == []


def test_render_run_py_vhdl_only():
    text = render_run_py([("cntlib", "/proj/counter.vhdl"), ("cntlib_tb", "tb.vhd")])
    assert "from vunit import VUnit" in text
    assert "vu.add_vhdl_builtins()" in text
    assert "vu.add_verilog_builtins()" not in text
    assert "cntlib = vu.add_library('cntlib')" in text
    assert "cntlib.add_source_files(\n    '/proj/counter.vhdl',\n)" in text
    assert "cntlib_tb.add_source_files(\n    'tb.vhd',\n)" in text
    # compile order preserved: cntlib before cntlib_tb
    assert text.index("cntlib = vu.add_library") < text.index(
        "cntlib_tb = vu.add_library"
    )
    assert text.rstrip().endswith("vu.main()")


def test_render_run_py_groups_files_per_library():
    text = render_run_py(
        [("work", "a.vhd"), ("work", "b.vhd"), ("tbs", "tb.vhd")]
    )
    block = "work.add_source_files(\n    'a.vhd',\n    'b.vhd',\n)"
    assert block in text
    assert text.count("add_source_files") == 2


def test_render_run_py_verilog_only():
    text = render_run_py([("vlib", "top.sv")])
    assert "vu.add_verilog_builtins()" in text
    assert "vu.add_vhdl_builtins()" not in text
