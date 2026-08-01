"""Phase 1b — the ReAct agent generates canonical traces to lint (spec §II.1)."""

from __future__ import annotations

from tracelint import SchemaViolationRule, lint_trace
from tracelint.agent import (
    AgentTool,
    AgentToolset,
    ReActAgent,
    ScriptedLLM,
    build_demo_toolset,
    final,
    run_demo,
    tool,
)
from tracelint.trace import Message, ResultStatus, Role, ToolResult


def _agent(script, toolset=None, **kw):
    toolset = toolset or build_demo_toolset()
    return ReActAgent(ScriptedLLM(script), toolset, **kw)


def test_clean_run_records_calls_results_and_final():
    trace, toolset = run_demo()
    assert trace.final and "cancelled" in trace.final.lower()
    assert [c.name for c in trace.tool_calls()] == ["get_order_status", "cancel_order"]
    # Every call is paired with an OK result.
    for _call, result in trace.pairs():
        assert result is not None and result.status is ResultStatus.OK
    # The last step is the assistant's final answer.
    assert isinstance(trace.steps[-1], Message) and trace.steps[-1].role is Role.ASSISTANT


def test_agent_pairs_call_and_result_by_id():
    trace = _agent([tool("get_order_status", {"order_id": "4521"}), final("done")]).run("t")
    call = trace.tool_calls()[0]
    result = trace.result_for(call)
    assert isinstance(result, ToolResult) and result.call_id == call.call_id


def test_tool_error_is_captured_with_http_status():
    # order 4521 IS shipped in this alternate: use 9001 which is shipped -> 409 on cancel.
    trace = _agent(
        [tool("cancel_order", {"order_id": "9001", "reason": "fraud"}), final("could not cancel")]
    ).run("cancel 9001")
    result = trace.tool_results()[0]
    assert result.status is ResultStatus.ERROR
    assert result.http_status == 409


def test_unknown_tool_yields_error_result_not_crash():
    trace = _agent([tool("nonexistent", {}), final("gave up")]).run("t")
    result = trace.tool_results()[0]
    assert result.status is ResultStatus.ERROR
    assert result.error == "unknown_tool"


def test_step_cap_terminates_a_nonstopping_agent():
    # A toolset whose only tool always succeeds, and a script that never emits FinalAnswer.
    calls = AgentToolset(
        [AgentTool("noop", "does nothing", {"type": "object"}, lambda a: {"ok": True})]
    )
    # Script shorter than max_steps; ScriptedLLM returns FinalAnswer once exhausted, so force a
    # loop by repeating tool proposals beyond the cap.
    script = [tool("noop", {}) for _ in range(20)]
    trace = ReActAgent(ScriptedLLM(script), calls, max_steps=3).run("loop forever")
    # Exactly max_steps tool calls, and no final answer (incomplete run).
    assert len(trace.tool_calls()) == 3
    assert trace.final is None


def test_thought_is_recorded_as_assistant_message():
    trace = _agent(
        [tool("get_order_status", {"order_id": "4521"}, thought="let me check"), final("ok")]
    ).run("t")
    assert any(
        isinstance(s, Message) and s.role is Role.ASSISTANT and s.content == "let me check"
        for s in trace.steps
    )


def test_toolset_registry_matches_called_tools():
    _trace, toolset = run_demo()
    registry = toolset.to_registry()
    assert registry.schema_for("cancel_order")["required"] == ["order_id", "reason"]
    assert registry.metadata_for("get_order_status").idempotent is True


def test_end_to_end_agent_trace_is_clean_under_r1():
    trace, toolset = run_demo()
    report = lint_trace(trace, [SchemaViolationRule()], toolset.to_registry())
    assert report.active_findings == []
    assert report.exit_code == 0


def test_end_to_end_planted_schema_violation_is_caught():
    toolset = build_demo_toolset()
    # Plant a defect: the model emits order_id as an integer, violating the string+pattern schema.
    bad_script = [
        tool("cancel_order", {"order_id": 4521, "reason": "fraud"}),
        final("I cancelled it."),
    ]
    trace = ReActAgent(ScriptedLLM(bad_script), toolset).run("cancel 4521", run_id="planted")
    report = lint_trace(trace, [SchemaViolationRule()], toolset.to_registry())
    assert report.exit_code == 2
    f = report.active_findings[0]
    assert f.rule == "R1" and f.evidence["tool"] == "cancel_order"
    assert any(e["keyword"] == "type" for e in f.evidence["errors"])


def test_openai_step_conversion_roundtrips_history():
    # OpenAILLM's message reconstruction should reproduce a valid OpenAI history from steps.
    from tracelint.agent.openai_llm import _steps_to_openai_messages

    trace = _agent([tool("get_order_status", {"order_id": "4521"}), final("done")]).run("t")
    msgs = _steps_to_openai_messages(trace.steps)
    roles = [m["role"] for m in msgs]
    assert "assistant" in roles and "tool" in roles
    call_msg = next(m for m in msgs if m.get("tool_calls"))
    assert call_msg["tool_calls"][0]["function"]["name"] == "get_order_status"
