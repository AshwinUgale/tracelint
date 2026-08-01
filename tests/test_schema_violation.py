"""Phase 1a — R1 schema violation (spec §II.5, R1)."""

from __future__ import annotations

from tracelint import (
    ConfidenceTier,
    SchemaViolationRule,
    ToolRegistry,
    ToolSpec,
    build_trace,
    lint_trace,
)
from tracelint.trace import Message, Role, ToolCall

CANCEL_SCHEMA = {
    "type": "object",
    "properties": {
        "order_id": {"type": "string", "pattern": "^[0-9]{4,10}$"},
        "reason": {"type": "string", "enum": ["customer_request", "duplicate", "fraud"]},
    },
    "required": ["order_id"],
    "additionalProperties": False,
}


def _registry() -> ToolRegistry:
    return ToolRegistry({"cancel_order": ToolSpec(name="cancel_order", schema=CANCEL_SCHEMA)})


def _lint(call: ToolCall, registry: ToolRegistry | None = None):
    trace = build_trace("r", [Message(Role.USER, "go"), call])
    return lint_trace(trace, [SchemaViolationRule()], registry or _registry())


def test_valid_call_produces_no_finding():
    report = _lint(ToolCall("c1", "cancel_order", {"order_id": "4521", "reason": "fraud"}))
    assert report.active_findings == []
    assert report.exit_code == 0


def test_type_and_enum_violations_are_a_hard_defect():
    # order_id is an int (should be string); reason is not in the enum.
    report = _lint(ToolCall("c1", "cancel_order", {"order_id": 4521, "reason": "because"}))
    findings = report.active_findings
    assert len(findings) == 1
    f = findings[0]
    assert f.tier is ConfidenceTier.HARD_DEFECT
    assert f.finding_type == "schema_violation"
    assert f.step_indices == [1]
    keywords = {e["keyword"] for e in f.evidence["errors"]}
    assert "type" in keywords and "enum" in keywords
    assert report.exit_code == 2


def test_missing_required_field_is_flagged():
    report = _lint(ToolCall("c1", "cancel_order", {"reason": "fraud"}))
    f = report.active_findings[0]
    assert any(e["keyword"] == "required" for e in f.evidence["errors"])


def test_additional_property_is_flagged():
    report = _lint(ToolCall("c1", "cancel_order", {"order_id": "4521", "sneaky": 1}))
    f = report.active_findings[0]
    assert any(e["keyword"] == "additionalProperties" for e in f.evidence["errors"])


def test_pattern_violation_is_flagged():
    report = _lint(ToolCall("c1", "cancel_order", {"order_id": "12"}))  # too short for pattern
    f = report.active_findings[0]
    assert any(e["keyword"] == "pattern" for e in f.evidence["errors"])


def test_errors_are_deterministically_ordered():
    call = ToolCall("c1", "cancel_order", {"order_id": 4521, "reason": "because", "x": 1})
    errs1 = _lint(call).active_findings[0].evidence["errors"]
    errs2 = _lint(call).active_findings[0].evidence["errors"]
    assert errs1 == errs2
    assert errs1 == sorted(errs1, key=lambda e: (e["path"], str(e["keyword"])))


def test_unknown_tool_is_a_per_call_suppression_not_a_defect():
    # Some tool has a schema (so the rule runs), but this call's tool is unknown.
    reg = _registry()
    trace = build_trace(
        "r",
        [ToolCall("c1", "cancel_order", {"order_id": "4521"}), ToolCall("c2", "mystery", {})],
    )
    report = lint_trace(trace, [SchemaViolationRule()], reg)
    assert report.active_findings == []  # cancel_order valid, mystery only suppressed
    assert len(report.suppressions) == 1
    assert "mystery" in report.suppressions[0].suppressed_reason
    assert report.exit_code == 0


def test_rule_suppressed_when_no_called_tool_has_a_schema():
    trace = build_trace("r", [ToolCall("c1", "mystery", {})])
    report = lint_trace(trace, [SchemaViolationRule()], ToolRegistry())
    assert len(report.suppressions) == 1
    assert report.active_findings == []


def test_rule_suppressed_when_no_tool_calls():
    trace = build_trace("r", [Message(Role.USER, "hello")])
    report = lint_trace(trace, [SchemaViolationRule()], _registry())
    assert report.suppressions[0].suppressed_reason == "trace has no tool calls to validate"


def test_invalid_tool_schema_suppresses_that_call():
    reg = ToolRegistry({"bad": ToolSpec(name="bad", schema={"type": "not-a-real-type"})})
    trace = build_trace("r", [ToolCall("c1", "bad", {"a": 1})])
    report = lint_trace(trace, [SchemaViolationRule()], reg)
    assert report.active_findings == []
    assert len(report.suppressions) == 1
    assert "invalid JSON Schema" in report.suppressions[0].suppressed_reason
