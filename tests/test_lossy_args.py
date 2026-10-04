"""Unknown tool-call arguments: a lossy record must not fail CI, and must not pass silently either.

Instrumentation often records a tool call's arguments lossily: as a bare value (LangChain's
single-input tools), redacted (OpenInference ``OPENINFERENCE_HIDE_INPUTS``), positionally with no
parameter names (``{"args": ["A100"], "kwargs": {}}`` from smolagents, Langfuse ``@observe`` and
LangSmith), or not at all (OTel GenAI without content capture). Up to 0.9 the adapters turned these
into ``{}`` or a made-up object, so R1 reported every required field missing, R6 called the record
malformed JSON, and the repeat rules compared made-up values. The result was exit 2 on valid runs.

The shared normalizer (``tracelint/adapters/_common.py``) now recovers the real arguments where the
trace has them (the ``tool_call`` the model emitted) and otherwise marks them *unknown*. The rules
skip unknown calls and disclose each one as not checked.

``fixtures/lossy_args`` holds real instrumentation output, generated offline with a scripted model
(no API calls): LangGraph 1.2 / langchain-core 1.6 instrumented by openinference-instrumentation-
langchain 0.1.76, and LangChain's LangSmith tracer with a stub client that records the run payloads
it would send.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tracelint import (
    ResultStatus,
    ToolCall,
    ToolRegistry,
    ToolResult,
    Trace,
    default_rules,
    from_langfuse_trace,
    from_langsmith_run,
    from_otel_spans,
    lint_trace,
)
from tracelint.adapters._common import tool_input_args
from tracelint.findings import ARGS_UNKNOWN, SUPPRESS_NOT_RECORDED, LintReport

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "lossy_args"


def _load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _registry(tools: dict[str, Any]) -> ToolRegistry:
    return ToolRegistry.from_dict({"tools": tools})


def _schema(*required: str, **types: str) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {name: {"type": t} for name, t in types.items()},
        "required": list(required),
    }


def _active(report: LintReport) -> list[str]:
    return sorted(f.rule for f in report.active_findings)


def _not_checked(report: LintReport) -> list[str]:
    """Rules that disclosed calls they skipped because the arguments are unknown."""
    return sorted(s.rule for s in report.suppressions if s.evidence.get("cause") == ARGS_UNKNOWN)


# --- Real lossy records: no false failure, and the gap is disclosed --------------------------


def test_lcel_string_tool_input_is_unknown_not_malformed():
    # `prompt | llm | parser | tool` records the tool's input as the bare string "TCK-9001", and no
    # LLM span emitted a tool_call to recover it from. 0.9 reported it as malformed JSON (R6) and,
    # with a schema, as missing `ticket_id` (R1): exit 2 on a valid run.
    trace = from_otel_spans(_load("lcel_string_tool_spans.json"))
    (call,) = trace.tool_calls()
    assert call.name == "lookup_ticket"
    assert call.raw_text is None
    assert "bare value" in call.args_unavailable

    registry = _registry({"lookup_ticket": {"schema": _schema("ticket_id", ticket_id="string")}})
    report = lint_trace(trace, default_rules(), registry)
    assert report.exit_code == 0
    assert _active(report) == []
    assert _not_checked(report) == ["R1", "R3"]


def test_redacted_inputs_are_unknown_not_malformed():
    # OPENINFERENCE_HIDE_INPUTS + _HIDE_OUTPUTS replace every input with "__REDACTED__", including
    # the tool_calls the model emitted. 0.9 reported each redacted call as malformed JSON (R6).
    trace = from_otel_spans(_load("langgraph_hide_inputs_outputs_spans.json"))
    calls = trace.tool_calls()
    assert [c.name for c in calls] == ["get_order", "refund_order"]
    assert all("redacted" in c.args_unavailable for c in calls)

    report = lint_trace(trace, default_rules())
    assert report.exit_code == 0
    assert "R6" not in _active(report)
    assert "R3" in _not_checked(report)


def test_redacted_tool_inputs_recover_the_arguments_the_model_emitted():
    # With only inputs hidden, the TOOL spans are redacted but the LLM spans still record the
    # model's tool_calls, so the real arguments are recovered rather than given up on.
    trace = from_otel_spans(_load("langgraph_hide_inputs_spans.json"))
    assert [(c.name, c.args, c.args_unavailable) for c in trace.tool_calls()] == [
        ("get_order", {"order_id": "A100"}, None),
        ("refund_order", {"order_id": "A100", "amount": 49.99}, None),
    ]


def test_each_tool_span_pairs_with_its_own_emitted_call():
    # LangChain records send_sms(phone=...) as the bare value "+15550001" but the two-argument call
    # as an object. 0.9 consumed the model's calls only for bare spans, so the third call got the
    # second call's arguments, and R8 reported a duplicate SMS that never happened.
    trace = from_otel_spans(_load("langgraph_optional_args_spans.json"))
    assert [c.args for c in trace.tool_calls()] == [
        {"phone": "+15550001"},
        {"phone": "+15550002", "text": "Delayed"},
        {"phone": "+15550003"},
    ]
    sms = _registry({"send_sms": {"metadata": {"side_effecting": True}}})
    assert "R8" not in _active(lint_trace(trace, default_rules(), sms))


def test_langsmith_string_tool_input_is_unknown():
    # LangSmith records a string tool's input as {"input": "TCK-9001"}. 0.9 used that as the
    # arguments, so a schema requiring `ticket_id` was a false R1 hard defect.
    trace = from_langsmith_run(_load("langsmith_lcel_run.json"))
    (call,) = trace.tool_calls()
    assert call.name == "lookup_ticket"
    assert "string-tool convention" in call.args_unavailable

    registry = _registry({"lookup_ticket": {"schema": _schema("ticket_id", ticket_id="string")}})
    report = lint_trace(trace, default_rules(), registry)
    assert report.exit_code == 0
    assert _active(report) == []
    assert "R1" in _not_checked(report)


# --- One normalizer: every adapter reads the same record the same way ------------------------


def _smolagents_positional() -> Trace:
    envelope = {"args": ["A100"], "sanitize_inputs_outputs": True, "kwargs": {}}
    tool = {
        "openinference.span.kind": "TOOL",
        "tool.name": "get_order",
        "input.value": json.dumps(envelope),
        "output.value": json.dumps({"order_id": "A100", "amount": 49.99}),
    }
    return from_otel_spans([{"name": "SimpleTool", "span_id": "s1", "attributes": tool}])


def _langfuse_positional() -> Trace:
    observation = {
        "id": "o1",
        "type": "tool",
        "name": "get_order",
        "input": {"args": ["A100"], "kwargs": {}},
        "output": {"order_id": "A100", "amount": 49.99},
    }
    return from_langfuse_trace({"id": "lf", "observations": [observation]})


def _langsmith_positional() -> Trace:
    run = {
        "id": "r1",
        "run_type": "tool",
        "name": "get_order",
        "inputs": {"args": ["A100"], "kwargs": {}},
        "outputs": {"order_id": "A100"},
    }
    return from_langsmith_run({"id": "ls", "run_type": "chain", "child_runs": [run]})


@pytest.mark.parametrize(
    "build",
    [_smolagents_positional, _langfuse_positional, _langsmith_positional],
    ids=["smolagents-openinference", "langfuse-observe", "langsmith"],
)
def test_positional_arguments_are_unknown_in_every_adapter(build):
    # get_order("A100") records the value without its parameter name. 0.9 read it as `{}` (or a
    # made-up {"args": [...]}), so a schema requiring `order_id` was a false R1 hard defect.
    (call,) = build().tool_calls()
    assert call.args == {}
    assert "positionally" in call.args_unavailable

    registry = _registry({"get_order": {"schema": _schema("order_id", order_id="string")}})
    report = lint_trace(build(), default_rules(), registry)
    assert report.exit_code == 0
    assert "R1" in _not_checked(report)


def test_a_positional_argument_object_keeps_its_names():
    # func({"order_id": "A100"}) puts the whole argument object in args[0]; its names are known.
    recorded = {"args": [{"order_id": "A100"}], "kwargs": {"verbose": True}}
    args = tool_input_args(recorded)
    assert args.args == {"order_id": "A100", "verbose": True}
    assert args.unavailable is None


def test_a_parameter_named_args_is_not_mistaken_for_an_envelope():
    # run_command(command, args) has a real `args` parameter. 0.9 mistook the object for a call
    # envelope and lost both arguments, which was a false R1.
    tool = {
        "openinference.span.kind": "TOOL",
        "tool.name": "run_command",
        "input.value": json.dumps({"command": "git", "args": ["status"]}),
        "output.value": "On branch main",
    }
    trace = from_otel_spans([{"name": "run_command", "span_id": "s1", "attributes": tool}])
    (call,) = trace.tool_calls()
    assert call.args == {"command": "git", "args": ["status"]}
    assert call.args_unavailable is None


def test_otel_genai_tool_arguments_and_result_are_read():
    # The OTel GenAI conventions record execute_tool content on gen_ai.tool.call.arguments and
    # .result. 0.9 read only input.value, so the call had no arguments (a false R1) and no result.
    tool = {
        "gen_ai.operation.name": "execute_tool",
        "gen_ai.tool.name": "cancel_order",
        "gen_ai.tool.call.id": "call_1",
        "gen_ai.tool.call.arguments": json.dumps({"order_id": "A100"}),
        "gen_ai.tool.call.result": json.dumps({"cancelled": True}),
    }
    trace = from_otel_spans([{"name": "execute_tool cancel_order", "attributes": tool}])
    (call,) = trace.tool_calls()
    assert call.args == {"order_id": "A100"}
    assert trace.result_for(call).content == {"cancelled": True}


def test_otel_genai_without_content_capture_names_the_opt_in():
    # GenAI instrumentations record tool content only when opted in; say how, rather than `{}`.
    tool = {"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": "cancel_order"}
    trace = from_otel_spans([{"name": "execute_tool cancel_order", "attributes": tool}])
    (call,) = trace.tool_calls()
    assert "not recorded" in call.args_unavailable
    assert "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT" in call.args_unavailable


# --- Recovery never launders a real defect ----------------------------------------------------


def _llm_then_tool(name: str, emitted: str, tool_input: str) -> list[dict[str, Any]]:
    """An LLM span that emits one tool_call, then the TOOL span that executed it."""
    prefix = "llm.output_messages.0.message."
    llm = {
        "openinference.span.kind": "LLM",
        prefix + "role": "assistant",
        prefix + "tool_calls.0.tool_call.id": "call_1",
        prefix + "tool_calls.0.tool_call.function.name": name,
        prefix + "tool_calls.0.tool_call.function.arguments": emitted,
    }
    tool = {
        "openinference.span.kind": "TOOL",
        "tool.name": name,
        "input.value": tool_input,
        "output.value": json.dumps({"ok": True}),
    }
    return [
        {"name": "llm", "span_id": "s1", "start_time": 1, "attributes": llm},
        {"name": name, "span_id": "s2", "start_time": 2, "attributes": tool},
    ]


def test_malformed_arguments_the_model_emitted_still_fail():
    # The TOOL span's bare input is recovered from the model's call, and that call's own text is
    # truncated JSON: that is model-emitted evidence, so R6 still fires.
    trace = from_otel_spans(_llm_then_tool("get_order", '{"order_id": "A100"', "A100"))
    (call,) = trace.tool_calls()
    assert call.raw_text == '{"order_id": "A100"'

    report = lint_trace(trace, default_rules())
    assert report.exit_code == 2
    assert "R6" in _active(report)


def test_recovered_arguments_are_still_schema_checked():
    # Arguments recovered from the model's call are real: a required field the model left out is
    # still a hard R1 defect.
    spans = _llm_then_tool("refund_order", json.dumps({"order_id": "A100"}), "A100")
    registry = _registry(
        {"refund_order": {"schema": _schema("order_id", "amount", order_id="string")}}
    )
    report = lint_trace(from_otel_spans(spans), default_rules(), registry)
    assert report.exit_code == 2
    assert "R1" in _active(report)


# --- Broken JSON in a tool's own record: shown for review, not failed -------------------------


def _tool_span(name: str, tool_input: str) -> dict[str, Any]:
    attrs = {"openinference.span.kind": "TOOL", "tool.name": name, "input.value": tool_input}
    return {"name": name, "span_id": "s1", "attributes": attrs}


def test_broken_json_in_a_tool_record_is_a_candidate_not_a_defect():
    # A long input cut by an exporter's attribute-length limit leaves this exact text, and nothing
    # here shows the model emitted it, so R6 shows it for review instead of failing CI. 0.9 failed
    # the run (exit 2).
    trace = from_otel_spans([_tool_span("write_file", '{"path": "app.py", "content": "impo')])
    report = lint_trace(trace, default_rules())
    assert report.exit_code == 0
    (r6,) = [f for f in report.active_findings if f.rule == "R6"]
    assert r6.tier.value == "candidate"
    assert r6.possible_false_positive
    assert r6.evidence["raw_arguments"] == '{"path": "app.py", "content": "impo'
    assert "truncated" in r6.summary


def test_the_models_own_call_settles_a_broken_tool_record():
    # The same broken record, but the model's emitted call is in the trace and is valid JSON: the
    # arguments are recovered, and there is nothing malformed to report.
    spans = _llm_then_tool("get_order", json.dumps({"order_id": "A100"}), '{"order_id": "A1')
    trace = from_otel_spans(spans)
    (call,) = trace.tool_calls()
    assert call.args == {"order_id": "A100"}
    assert "R6" not in _active(lint_trace(trace, default_rules()))


def test_broken_json_in_a_langfuse_tool_observation_is_a_candidate():
    # The adapters share one normalizer, so Langfuse reads the same record the same way.
    observation = {"id": "o1", "type": "tool", "name": "get_order", "input": '{"order_id": '}
    report = lint_trace(
        from_langfuse_trace({"id": "lf", "observations": [observation]}), default_rules()
    )
    assert report.exit_code == 0
    assert [f.tier.value for f in report.active_findings if f.rule == "R6"] == ["candidate"]


# --- Rules that compare or trace argument values disclose what they skipped -------------------


def _unknown_call(call_id: str, name: str) -> ToolCall:
    return ToolCall(call_id, name, args_unavailable="the tool's input was redacted")


def test_a_repeated_side_effect_with_unknown_arguments_is_disclosed():
    # Two charges with redacted inputs may be a double charge or two different orders. R8 cannot
    # tell, so the report must say so instead of reading as "no duplicate".
    trace = Trace(
        run_id="charges",
        steps=[
            _unknown_call("c1", "charge_card"),
            ToolResult("c1", {"charged": True}, ResultStatus.OK),
            _unknown_call("c2", "charge_card"),
            ToolResult("c2", {"charged": True}, ResultStatus.OK),
        ],
    )
    charge = _registry({"charge_card": {"metadata": {"side_effecting": True}}})
    report = lint_trace(trace, default_rules(), charge)
    assert "R8" not in _active(report)
    (disclosure,) = [s for s in report.suppressions if s.rule == "R8"]
    assert disclosure.evidence["cause"] == ARGS_UNKNOWN
    assert disclosure.suppressed_category == SUPPRESS_NOT_RECORDED
    assert disclosure.step_indices == [0, 2]
    assert "2 calls to 'charge_card' not checked for duplicate side effects" in (
        disclosure.suppressed_reason
    )


def test_a_call_after_a_failed_result_with_unknown_arguments_is_disclosed():
    # The pipeline failed and then a deploy ran with redacted arguments; whether the deploy reused
    # the failed build cannot be checked, which is not the same as "it did not".
    trace = Trace(
        run_id="deploy",
        steps=[
            ToolCall("p1", "run_pipeline", {"ref": "main"}),
            ToolResult("p1", {"result": "UNSTABLE", "build": "b-117"}, ResultStatus.OK),
            _unknown_call("d1", "deploy"),
            ToolResult("d1", {"status": "ok"}, ResultStatus.OK),
        ],
    )
    registry = _registry(
        {
            "run_pipeline": {
                "metadata": {"failure_when": {"pointer": "/result", "equals": "UNSTABLE"}}
            },
            "deploy": {"metadata": {"side_effecting": True}},
        }
    )
    report = lint_trace(trace, default_rules(), registry)
    assert "R2b" in _not_checked(report)


def test_a_possible_loop_with_unknown_arguments_is_disclosed():
    steps: list[ToolCall | ToolResult] = []
    for i in range(3):
        steps += [_unknown_call(f"s{i}", "search"), ToolResult(f"s{i}", [], ResultStatus.OK)]
    report = lint_trace(Trace(run_id="loop", steps=steps), default_rules())
    assert "R4" not in _active(report)
    assert "R4" in _not_checked(report)


def test_known_arguments_produce_no_disclosure():
    # The disclosure is only for calls that were actually skipped.
    trace = from_otel_spans(_load("langgraph_optional_args_spans.json"))
    sms = _registry({"send_sms": {"metadata": {"side_effecting": True}}})
    assert _not_checked(lint_trace(trace, default_rules(), sms)) == []


def test_unknown_arguments_survive_a_native_round_trip():
    # `tracelint check` reads native JSON; the unknown state must not decay back into `{}`.
    trace = from_langsmith_run(_load("langsmith_lcel_run.json"))
    again = Trace.from_dict(json.loads(json.dumps(trace.to_dict())))
    (call,) = again.tool_calls()
    assert call.args_unavailable == trace.tool_calls()[0].args_unavailable
