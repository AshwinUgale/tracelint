"""Verification coverage — per-rule "how much could I actually evaluate?" (Item 3).

Coverage makes abstention legible: it reports, per rule, how many units (tool calls, tool results)
were structurally checkable vs. abstained on. That is *why* a clean report is trustworthy — a reader
can see how much was verified, not merely that nothing fired.
"""

from __future__ import annotations

from tracelint import ToolRegistry, Trace, lint_trace
from tracelint.report import render_report
from tracelint.rules import SchemaViolationRule, ToolErrorEventRule


def _cov(report, rule):
    return next(c for c in report.coverage if c.rule == rule)


def test_r1_coverage_counts_calls_with_a_declared_schema():
    reg = ToolRegistry.from_dict({"tools": {"a": {"schema": {"type": "object"}}}})  # b: no schema
    trace = Trace.from_dict(
        {
            "run_id": "x",
            "steps": [
                {"type": "tool_call", "call_id": "1", "name": "a", "args": {}},
                {"type": "tool_call", "call_id": "2", "name": "a", "args": {}},
                {"type": "tool_call", "call_id": "3", "name": "b", "args": {}},
            ],
        }
    )
    cov = _cov(lint_trace(trace, [SchemaViolationRule()], reg), "R1")
    assert (cov.evaluatable, cov.total, cov.unit) == (2, 3, "tool calls")


def test_r2a_coverage_counts_structurally_classifiable_results():
    reg = ToolRegistry.from_dict(
        {
            "tools": {
                "c": {"metadata": {"failure_when": {"pointer": "/status", "in": ["declined"]}}},
                "d": {"metadata": {"failure_when": {"pointer": "/status", "in": ["declined"]}}},
            }
        }
    )
    steps: list[dict] = []

    def pair(cid, name, content, **result_kw):
        steps.append({"type": "tool_call", "call_id": cid, "name": name, "args": {}})
        steps.append({"type": "tool_result", "call_id": cid, "content": content, **result_kw})

    pair("1", "a", "boom", status="ok", http_status=500)  # structured error → evaluatable
    pair("2", "b", "ok", status="ok")  # explicit OK, no contract → evaluatable
    pair("3", "c", {"status": "declined"}, status="ok")  # failure_when MATCH → evaluatable
    pair("4", "d", {"amount": 5}, status="ok")  # failure_when UNKNOWN (field absent) → NOT
    trace = Trace.from_dict({"run_id": "x", "steps": steps})

    cov = _cov(lint_trace(trace, [ToolErrorEventRule()], reg), "R2a")
    assert (cov.evaluatable, cov.total, cov.unit) == (3, 4, "tool results")


def test_whole_rule_suppression_shows_as_zero_coverage():
    # No schema for any called tool → R1 is whole-rule suppressed, and coverage reads 0/total.
    reg = ToolRegistry.from_dict({"tools": {}})
    trace = Trace.from_dict(
        {"run_id": "x", "steps": [{"type": "tool_call", "call_id": "1", "name": "a", "args": {}}]}
    )
    report = lint_trace(trace, [SchemaViolationRule()], reg)
    assert _cov(report, "R1").evaluatable == 0
    assert report.suppressions  # still disclosed as a suppression, too


def test_ratio_render_and_to_dict():
    reg = ToolRegistry.from_dict({"tools": {"a": {"schema": {"type": "object"}}}})
    trace = Trace.from_dict(
        {
            "run_id": "x",
            "steps": [
                {"type": "tool_call", "call_id": "1", "name": "a", "args": {}},
                {"type": "tool_call", "call_id": "2", "name": "b", "args": {}},
            ],
        }
    )
    report = lint_trace(trace, [SchemaViolationRule()], reg)
    assert _cov(report, "R1").ratio == 0.5
    out = render_report(report)
    assert "verification coverage" in out and "R1  1/2 tool calls" in out
    assert report.to_dict()["coverage"][0] == {
        "rule": "R1",
        "unit": "tool calls",
        "evaluatable": 1,
        "total": 2,
    }


def test_rules_without_coverage_leave_it_empty_and_out_of_to_dict():
    report = lint_trace(Trace.from_dict({"run_id": "x", "steps": []}), [], None)
    assert report.coverage == []
    assert "coverage" not in report.to_dict()
