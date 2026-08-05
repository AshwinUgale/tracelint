"""R6 malformed arguments + R7 unknown tool."""

from __future__ import annotations

from tracelint import (
    ConfidenceTier,
    MalformedArgumentsRule,
    ToolRegistry,
    ToolSpec,
    UnknownToolRule,
    lint_trace,
)
from tracelint.trace import Message, Role, ToolCall, Trace


def _trace(*steps):
    return Trace(run_id="t", steps=list(steps))


# --- R6 malformed arguments ------------------------------------------------------------


def test_r6_flags_invalid_json_arguments_as_hard_defect():
    trace = _trace(
        Message(Role.USER, "cancel order 4521"),
        ToolCall("c1", "cancel_order", {}, raw_text='{"order_id": "45'),  # truncated JSON
    )
    report = lint_trace(trace, [MalformedArgumentsRule()], ToolRegistry())
    findings = report.active_findings
    assert len(findings) == 1
    assert findings[0].rule == "R6" and findings[0].tier is ConfidenceTier.HARD_DEFECT
    assert report.exit_code == 2


def test_r6_ignores_valid_json_and_empty_calls():
    trace = _trace(
        ToolCall("c1", "t", {"order_id": "A100"}, raw_text='{"order_id": "A100"}'),
        ToolCall("c2", "t", {}, raw_text="{}"),  # a valid empty object
        ToolCall("c3", "t", {}),  # no raw_text at all
    )
    report = lint_trace(trace, [MalformedArgumentsRule()], ToolRegistry())
    assert report.active_findings == []


def test_r6_suppressed_when_no_tool_calls():
    report = lint_trace(_trace(Message(Role.USER, "hi")), [MalformedArgumentsRule()])
    assert report.suppressions and not report.active_findings


# --- R7 unknown tool -------------------------------------------------------------------


def test_r7_flags_undeclared_tool_as_candidate():
    trace = _trace(ToolCall("c1", "teleport_order", {"order_id": "4521"}))
    registry = ToolRegistry({"cancel_order": ToolSpec("cancel_order")})
    report = lint_trace(trace, [UnknownToolRule()], registry)
    findings = report.active_findings
    assert len(findings) == 1
    assert findings[0].rule == "R7" and findings[0].tier is ConfidenceTier.CANDIDATE
    assert findings[0].possible_false_positive
    assert report.exit_code == 0  # candidate never fails CI


def test_r7_silent_when_tool_is_declared():
    trace = _trace(ToolCall("c1", "cancel_order", {"order_id": "4521"}))
    registry = ToolRegistry({"cancel_order": ToolSpec("cancel_order")})
    report = lint_trace(trace, [UnknownToolRule()], registry)
    assert report.active_findings == []


def test_r7_suppressed_without_a_registry():
    trace = _trace(ToolCall("c1", "whatever", {}))
    report = lint_trace(trace, [UnknownToolRule()], ToolRegistry())
    assert report.suppressions and not report.active_findings
