"""Phase 0 — the fail-closed driver: a rule that cannot run is disclosed, not skipped.

This is the load-bearing behaviour of the whole tool (deep-design Trap 1 / spec §II.9): a rule
whose required data is missing must produce a *suppression* with a stated reason, never a silent
clean pass. The concrete rules arrive later; here we prove the contract with tiny in-test rules.
"""

from __future__ import annotations

from tracelint import (
    ConfidenceTier,
    Finding,
    Message,
    Role,
    Rule,
    ToolCall,
    ToolRegistry,
    Trace,
    build_trace,
    lint_trace,
)
from tracelint.tools import ToolSpec


class _AlwaysFires(Rule):
    id = "RX"
    finding_type = "demo_defect"

    def run(self, trace, registry):
        return [
            Finding(
                self.id,
                self.finding_type,
                ConfidenceTier.HARD_DEFECT,
                "fired",
                {"step_indices": [0]},
            )
        ]


class _NeedsSchema(Rule):
    """Runs only if every called tool is in the registry — else suppresses with a reason."""

    id = "R1"
    finding_type = "schema_violation"

    def applicable(self, trace: Trace, registry: ToolRegistry) -> str | None:
        missing = sorted({c.name for c in trace.tool_calls() if c.name not in registry})
        if missing:
            return f"no schema for tool(s): {', '.join(missing)}"
        return None

    def run(self, trace, registry):
        return []  # no violations in this fixture


def _trace_with_call() -> Trace:
    return build_trace("run", [Message(Role.USER, "hi"), ToolCall("c1", "mystery_tool", {})])


def test_runnable_rule_produces_findings():
    report = lint_trace(_trace_with_call(), [_AlwaysFires()])
    assert len(report.active_findings) == 1
    assert report.exit_code == 2


def test_missing_ground_truth_suppresses_with_reason():
    # mystery_tool is unknown → R1 must suppress, not silently pass.
    report = lint_trace(_trace_with_call(), [_NeedsSchema()], ToolRegistry())
    assert report.active_findings == []
    assert len(report.suppressions) == 1
    supp = report.suppressions[0]
    assert supp.rule == "R1"
    assert "mystery_tool" in supp.suppressed_reason
    assert report.exit_code == 0  # cannot-check is not a defect


def test_rule_runs_once_ground_truth_is_present():
    registry = ToolRegistry(
        {"mystery_tool": ToolSpec(name="mystery_tool", schema={"type": "object"})}
    )
    report = lint_trace(_trace_with_call(), [_NeedsSchema()], registry)
    assert report.suppressions == []
    assert report.active_findings == []  # ran, found nothing


def test_rule_order_is_preserved():
    report = lint_trace(_trace_with_call(), [_AlwaysFires(), _NeedsSchema()], ToolRegistry())
    assert [f.rule for f in report.findings] == ["RX", "R1"]


def test_suppression_and_defect_coexist_in_one_report():
    report = lint_trace(_trace_with_call(), [_AlwaysFires(), _NeedsSchema()], ToolRegistry())
    assert report.has_hard_defect is True
    assert len(report.suppressions) == 1
