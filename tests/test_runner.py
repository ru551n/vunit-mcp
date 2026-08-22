"""Unit tests for RunResult.summary truncation (no simulator needed)."""

from vunit_mcp.runner import RunResult


def test_summary_short_untouched():
    r = RunResult(returncode=0, stdout="ok", stderr="", argv=[])
    assert r.summary() == "ok"


def test_summary_tail_truncation_keeps_the_end():
    r = RunResult(
        returncode=1,
        stdout="\n".join(f"line {i}" for i in range(1000)),
        stderr="final error line",
        argv=[],
    )
    text = r.summary(max_chars=500)
    assert text.endswith("final error line")
    assert "line 999" in text
    assert "line 0" not in text
    assert "truncated" in text
    assert len(text) <= 500 + 100  # marker line overhead


def test_summary_stderr_included():
    r = RunResult(returncode=1, stdout="out", stderr="err", argv=[])
    text = r.summary()
    assert "out" in text and "err" in text
