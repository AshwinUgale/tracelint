"""Suppressions carry a category so a report can advise the right fix (audit 2d / 3.7).

A rule that abstains is always disclosed, but *why* it abstained changes what the reader should do:
nothing (the trace can't trigger it), declare a tools.json fact, or fix their instrumentation. The
category rides on the finding and drives both the grouped text report and the JSON output.
"""

from __future__ import annotations

from tracelint import ToolRegistry, build_trace, lint_trace
from tracelint.findings import SUPPRESS_NEEDS_CONTRACT, SUPPRESS_NOT_APPLICABLE
from tracelint.rules import select_rules
from tracelint.trace import ToolCall, ToolResult


def _cats(report, rule):
    return {s.suppressed_category for s in report.suppressions if s.rule == rule}


def test_too_short_to_trigger_is_not_applicable():
    # One tool call: R4/R5/R8 structurally cannot fire — nothing to check, not a missing contract.
    trace = build_trace("t", [ToolCall("c1", "get", {"id": "A"}), ToolResult("c1", {"ok": True})])
    report = lint_trace(trace, select_rules(["R4", "R5", "R8"]), ToolRegistry())
    assert report.suppressions
    assert {s.suppressed_category for s in report.suppressions} == {SUPPRESS_NOT_APPLICABLE}


def test_missing_schema_needs_a_contract_and_serialises():
    # No registry: R1 can't validate — the fix is a tools.json, not instrumentation.
    trace = build_trace("t", [ToolCall("c1", "pay", {"amt": 5}), ToolResult("c1", {"ok": True})])
    report = lint_trace(trace, select_rules(["R1"]), ToolRegistry())
    assert _cats(report, "R1") == {SUPPRESS_NEEDS_CONTRACT}
    (supp,) = report.to_dict()["suppressions"]
    assert supp["suppressed_category"] == SUPPRESS_NEEDS_CONTRACT
