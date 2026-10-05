"""R11 — contract drift: the run-time tool schema vs the committed tools.json (audit 3.3).

Some traces carry each tool's schema inline (OTel / OpenInference definitions, an OpenAI tools
block). When that run-time schema differs structurally from the committed contract, the contract has
gone stale. R11 flags it as a `hard_event` — a certain fact about the contract, not a defect in the
run — shown but not failing CI unless the operator opts in. Opt-in and structural: cosmetic edits,
a missing side, or a property-less placeholder stay silent.
"""

from __future__ import annotations

from tracelint import ToolRegistry, build_trace, lint_trace
from tracelint.findings import ConfidenceTier
from tracelint.rules import rule_ids, select_rules
from tracelint.trace import ResultStatus, ToolCall, ToolResult

CONTRACT = {
    "type": "object",
    "properties": {"order_id": {"type": "string"}},
    "required": ["order_id"],
}


def _trace(inline_schema):
    call = ToolCall("c1", "get_order", {"order_id": "A"})
    call.schema = inline_schema
    return build_trace("t", [call, ToolResult("c1", {"ok": True}, status=ResultStatus.OK)])


def _reg(schema=CONTRACT):
    spec = {"schema": schema} if schema is not None else {}
    return ToolRegistry.from_dict({"tools": {"get_order": spec}})


def _r11(trace, registry):
    return lint_trace(trace, select_rules(["R11"]), registry)


def test_r11_is_registered():
    assert "R11" in rule_ids()


def test_drift_is_a_hard_event_with_the_diff():
    inline = {
        "type": "object",
        "properties": {"order_id": {"type": "integer"}, "region": {"type": "string"}},
    }
    (f,) = _r11(_trace(inline), _reg()).active_findings
    assert f.rule == "R11" and f.tier is ConfidenceTier.HARD_EVENT
    assert f.finding_type == "contract_drift"
    assert f.evidence["drift"]["added_in_run"] == ["region"]  # the live tool gained a field
    assert f.evidence["drift"]["retyped"] == ["order_id"]  # string -> integer


def test_an_identical_schema_is_clean():
    assert _r11(_trace(dict(CONTRACT)), _reg()).active_findings == []


def test_a_cosmetic_difference_does_not_flag():
    inline = {
        "title": "GetOrder",  # titles / descriptions / order are ignored
        "type": "object",
        "properties": {"order_id": {"type": "string", "description": "the order id"}},
        "required": ["order_id"],
    }
    assert _r11(_trace(inline), _reg()).active_findings == []


def test_drift_does_not_fail_ci_by_default_but_does_under_fail_on():
    rep = _r11(_trace({"type": "object", "properties": {"region": {"type": "string"}}}), _reg())
    assert rep.exit_code == 0  # hard_event: shown, not a default CI failure
    rep.fail_on = ConfidenceTier.HARD_EVENT
    assert rep.exit_code == 1


def test_no_inline_schema_is_dormant():
    trace = build_trace(
        "t", [ToolCall("c1", "get_order", {"order_id": "A"}), ToolResult("c1", {"ok": True})]
    )
    r = _r11(trace, _reg())
    assert r.active_findings == [] and r.suppressions == []


def test_no_committed_schema_is_dormant():
    inline = {"type": "object", "properties": {"region": {"type": "string"}}}
    r = _r11(_trace(inline), _reg(schema=None))
    assert r.active_findings == [] and r.suppressions == []


def test_a_propertyless_schema_is_not_compared():
    # a bare {"type": "object"} placeholder on either side is not a real shape to diff
    assert _r11(_trace({"type": "object"}), _reg()).active_findings == []
