"""LangSmith adapter — normalize nested runs into the canonical schema."""

from __future__ import annotations

from tracelint import (
    ErrorHandlingRule,
    ToolErrorEventRule,
    default_rules,
    from_langsmith_run,
    lint_trace,
)
from tracelint.findings import ConfidenceTier
from tracelint.tools import ToolRegistry
from tracelint.trace import Message, ResultStatus, Role, ToolCall, ToolResult


def _tool_run(rid, name, inputs, outputs=None, *, error=None, start="2024-01-01T00:00:01Z"):
    run = {"id": rid, "run_type": "tool", "name": name, "inputs": inputs, "start_time": start}
    if outputs is not None:
        run["outputs"] = outputs
    if error is not None:
        run["error"] = error
    return run


def _langsmith_run():
    return {
        "id": "root",
        "run_type": "chain",
        "inputs": {"input": "Cancel order Z999."},
        "outputs": {"output": "done"},
        "child_runs": [
            _tool_run(
                "cancel",
                "cancel_order",
                {"order_id": "Z999"},
                {"status": "ok"},
                start="2024-01-01T00:00:02Z",
            ),
            _tool_run(
                "lookup",
                "lookup_order",
                {"order_id": "Z999"},
                {"order_id": "Z999", "status": "missing"},
                error="order not found",
                start="2024-01-01T00:00:01Z",
            ),
        ],
    }


def test_nested_tool_runs_become_ordered_calls_and_results():
    trace = from_langsmith_run(_langsmith_run())

    assert trace.run_id == "root"
    assert isinstance(trace.steps[0], Message) and trace.steps[0].role is Role.USER
    assert [c.name for c in trace.tool_calls()] == ["lookup_order", "cancel_order"]

    call = trace.tool_calls()[0]
    assert isinstance(call, ToolCall) and call.args == {"order_id": "Z999"}
    result = trace.result_for(call)
    assert isinstance(result, ToolResult)
    assert result.status is ResultStatus.ERROR
    assert result.error == "order not found"


def _failed_lookup_then_cancel() -> dict:
    # lookup_order fails, but its response still carries a payment reference that the
    # side-effecting cancel_order then uses: a value that came only from the failed result.
    run = _langsmith_run()
    lookup, cancel = run["child_runs"][1], run["child_runs"][0]
    lookup["outputs"] = {"order_id": "Z999", "status": "missing", "payment_ref": "pay_4410"}
    cancel["inputs"] = {"order_id": "Z999", "payment_ref": "pay_4410"}
    return run


def test_tool_error_survives_adapter_for_r2_localization():
    registry = ToolRegistry.from_dict(
        {
            "tools": {
                "lookup_order": {},
                "cancel_order": {"metadata": {"side_effecting": True}},
            }
        }
    )
    report = lint_trace(
        from_langsmith_run(_failed_lookup_then_cancel()),
        [ToolErrorEventRule(), ErrorHandlingRule()],
        registry,
    )

    assert any(
        f.rule == "R2a"
        and f.tier is ConfidenceTier.HARD_EVENT
        and f.evidence["tool"] == "lookup_order"
        for f in report.active_findings
    )
    assert any(
        f.rule == "R2b"
        and f.tier is ConfidenceTier.HARD_DEFECT
        and f.evidence["consumer"] == "cancel_order"
        for f in report.active_findings
    )


def test_missing_output_fails_closed_as_unknown_result():
    trace = from_langsmith_run(_tool_run("lookup", "lookup_order", {"order_id": "Z999"}))

    result = trace.tool_results()[0]
    assert result.content is None
    assert result.status is ResultStatus.UNKNOWN


def test_execution_order_integers_sort_numerically():
    # Runs whose only ordering signal is the integer execution_order must sort
    # numerically (1, 2, 10), not lexically as strings ("1", "10", "2").
    run = {
        "id": "root",
        "run_type": "chain",
        "child_runs": [
            {"id": "c10", "run_type": "tool", "name": "t10", "inputs": {}, "execution_order": 10},
            {"id": "c2", "run_type": "tool", "name": "t2", "inputs": {}, "execution_order": 2},
            {"id": "c1", "run_type": "tool", "name": "t1", "inputs": {}, "execution_order": 1},
        ],
    }
    trace = from_langsmith_run(run)
    assert [c.name for c in trace.tool_calls()] == ["t1", "t2", "t10"]


def test_positional_tool_args_are_unknown_not_empty():
    # {"args": ["Z999"], "kwargs": {}} records a positional value with no parameter name. It must
    # not collapse to `{}` (read as "every required field missing") or a made-up {"args": [...]}:
    # the arguments are unknown, and R1 discloses that instead of failing CI on a valid call.
    trace = from_langsmith_run(_tool_run("t", "do", {"args": ["Z999"], "kwargs": {}}))

    call = trace.tool_calls()[0]
    assert call.args == {}
    assert call.args_unavailable and "positionally" in call.args_unavailable

    registry = ToolRegistry.from_dict(
        {
            "do": {
                "schema": {
                    "type": "object",
                    "properties": {"id": {"type": "string"}},
                    "required": ["id"],
                }
            }
        }
    )
    report = lint_trace(trace, default_rules(), registry)
    assert not report.has_hard_defect
    assert any(
        f.rule == "R1" and "arguments unknown" in (f.suppressed_reason or "")
        for f in report.suppressions
    )


def test_run_level_http_status_flags_error():
    # A numeric HTTP status at the run level (no error field, no outputs dict) is an
    # error signal, just like one inside outputs.
    run = _tool_run("t", "do", {"q": "x"})
    run["status_code"] = 500
    trace = from_langsmith_run(run)

    result = trace.tool_results()[0]
    assert result.status is ResultStatus.ERROR
    assert result.http_status == 500
