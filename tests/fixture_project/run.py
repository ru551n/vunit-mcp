#!/usr/bin/env python3
"""Minimal VUnit project used as a fixture by vunit-mcp's tests.

Two test benches:

- tb.tb_counter      -- two passing test cases
- tb.tb_counter_fail -- one test case with a failing check (for exercising
  failure paths: report, logs, check results)

Run manually with:  python run.py
"""

from pathlib import Path

from vunit import VUnit

HERE = Path(__file__).parent

PROJ = VUnit.from_argv()
# VUnit 5: HDL builtins are no longer compiled by default (issue #777).
PROJ.add_vhdl_builtins()
PROJ.add_library("rtl")
PROJ.add_source_file(HERE / "rtl" / "counter.vhd", library_name="rtl")
PROJ.add_library("tb")
PROJ.add_source_file(HERE / "tb" / "tb_counter.vhd", library_name="tb")
PROJ.add_source_file(HERE / "tb" / "tb_counter_fail.vhd", library_name="tb")

RESULT = PROJ.main()
raise SystemExit(0 if RESULT else 1)
