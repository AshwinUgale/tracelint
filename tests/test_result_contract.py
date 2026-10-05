"""R10 — result contract: a tool result must satisfy its declared output_schema (audit 3.3).

The mirror of R1 (which checks the arguments the model emitted), but on the tool's output. A
violation is a `hard_event` — a certain fact, the tool's output drifting from its contract, not the
agent's own defect — so it is shown but does not fail CI unless the operator opts into it. Opt-in: a
tool with no output_schema is completely silent.
"""

from __future__ import annotations

from tracelint import ToolRegistry, build_trace, lint_trace
from tracelint.findings import SUPPRESS_NEEDS_CONTRACT, SUPPRESS_NOT_RECORDED, ConfidenceTier
from tracelint.rules import rule_ids, select_rules
from tracelint.trace import ResultStatus, ToolCall, ToolResult

_SCHEMA = {"type": "object", "properties": {"total": {"type": "number"}}, "required": ["total"]}


def _reg(output_schema=_SCHEMA, **extra):
    spec = dict(extra)
    if output_schema is not None:
        spec["output_schema"] = output_schema
    return ToolRegistry.from_dict({"tools": {"get_order": spec}})


def _trace(content, status=ResultStatus.OK):
    return build_trace(
        "t", [ToolCall("c1", "get_order", {}), ToolResult("c1", content, status=status)]
    )


def _r10(trace, registry):
    return lint_trace(trace, select_rules(["R10"]), registry)


def test_r10_is_registered():
    assert "R10" in rule_ids()


def test_result_matching_the_schema_is_clean():
    r = _r10(_trace({"total": 42}), _reg())
    assert r.active_findings == [] and r.suppressions == []


def test_result_violating_the_schema_is_a_hard_event():
    (f,) = _r10(_trace({"id": "A"}), _reg()).active_findings  # missing required 'total'
    assert f.rule == "R10" and f.tier is ConfidenceTier.HARD_EVENT
    assert f.finding_type == "result_contract_violation" and "output_schema" in f.summary


def test_a_violation_does_not_fail_ci_by_default_but_does_under_fail_on():
    rep = _r10(_trace({"id": "A"}), _reg())
    assert rep.exit_code == 0  # hard_event: shown, not a default CI failure
    rep.fail_on = ConfidenceTier.HARD_EVENT
    assert rep.exit_code == 1  # the operator can opt into gating on it


def test_a_structured_error_result_is_skipped():
    # An error isn't the success shape output_schema describes — that's R2's domain.
    r = _r10(_trace({"id": "A"}, status=ResultStatus.ERROR), _reg())
    assert r.active_findings == [] and r.suppressions == []


def test_a_declared_failure_is_skipped():
    reg = _reg(metadata={"failure_when": {"pointer": "/ok", "equals": False}})
    r = _r10(_trace({"ok": False}), reg)  # a declared failure that also violates output_schema
    assert r.active_findings == [] and r.suppressions == []  # R2's domain, not output drift


def test_unrecorded_content_is_disclosed_not_validated():
    (s,) = _r10(_trace(None), _reg()).suppressions
    assert s.rule == "R10" and s.suppressed_category == SUPPRESS_NOT_RECORDED


def test_an_invalid_output_schema_is_suppressed():
    (s,) = _r10(_trace({"total": 1}), _reg(output_schema={"type": "nonsense"})).suppressions
    assert s.suppressed_category == SUPPRESS_NEEDS_CONTRACT


def test_a_tool_without_output_schema_is_dormant():
    r = _r10(_trace({"id": "A"}), _reg(output_schema=None))
    assert r.active_findings == [] and r.suppressions == [] and r.coverage == []
