"""The Langfuse cookbook example — its offline path, driven deterministically (no key)."""

from __future__ import annotations

from examples.langfuse_cookbook import (
    SAMPLE_TOOL_NAMES,
    SAMPLE_TOOLS,
    SAMPLE_TRACE,
    lint_langfuse,
    main,
)
from tracelint import ConfidenceTier, ToolRegistry


def test_sample_trace_lints_to_a_hard_defect():
    registry = ToolRegistry.from_dict(SAMPLE_TOOLS)
    report = lint_langfuse(SAMPLE_TRACE, registry=registry, tool_names=SAMPLE_TOOL_NAMES)
    assert report.has_hard_defect
    assert report.exit_code == 2
    # the malformed refund amount (string where the schema requires a number) is the hard defect
    defects = report.by_tier(ConfidenceTier.HARD_DEFECT)
    assert any(f.rule == "R1" for f in defects)


def test_main_offline_returns_exit_two(capsys):
    code = main([])  # no --trace-id → the bundled offline sample
    assert code == 2
    out = capsys.readouterr().out
    assert "offline sample" in out
