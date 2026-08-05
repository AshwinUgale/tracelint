"""Langfuse adapter — normalize Langfuse traces into the canonical schema."""

from __future__ import annotations

from tracelint import (
    SchemaViolationRule,
    from_langfuse_trace,
    lint_trace,
    observed_tool_names,
)
from tracelint.tools import ToolRegistry
from tracelint.trace import Message, ResultStatus, Role, ToolCall, ToolResult


def _tool_obs(
    oid, name, inp, out, *, level=None, status_message=None, start="2024-01-01T00:00:01Z"
):
    obs = {"id": oid, "type": "tool", "name": name, "input": inp, "output": out, "startTime": start}
    if level is not None:
        obs["level"] = level
    if status_message is not None:
        obs["statusMessage"] = status_message
    return obs


def test_native_tool_observation_becomes_paired_call_and_result():
    trace = from_langfuse_trace(
        {
            "id": "t1",
            "input": "refund order A100",
            "output": "done",
            "observations": [
                _tool_obs("o1", "lookup_order", {"order_id": "A100"}, {"amount": 49.99})
            ],
        }
    )
    assert trace.run_id == "t1"
    call = trace.tool_calls()[0]
    assert isinstance(call, ToolCall) and call.name == "lookup_order"
    assert call.args == {"order_id": "A100"}
    result = trace.result_for(call)
    assert isinstance(result, ToolResult) and result.content == {"amount": 49.99}
    assert trace.final == "done"
    # leading user message seeded from the trace input
    assert isinstance(trace.steps[0], Message) and trace.steps[0].role is Role.USER


def test_span_named_tool_recognized_via_tool_names():
    obs = {
        "id": "o1",
        "type": "span",
        "name": "issue_refund",
        "input": {"order_id": "A100", "amount": 49.99},
        "output": {"refunded": True},
    }
    trace = from_langfuse_trace({"id": "t", "observations": [obs]}, tool_names=["issue_refund"])
    assert trace.tool_calls()[0].name == "issue_refund"
    assert trace.tool_results()[0].content == {"refunded": True}


def test_span_not_in_tool_names_is_skipped():
    obs = {
        "id": "o1",
        "type": "span",
        "name": "retrieval",
        "input": {"q": "x"},
        "output": {"docs": []},
    }
    trace = from_langfuse_trace({"id": "t", "observations": [obs]})
    assert trace.tool_calls() == []  # arbitrary spans are not invented into tool calls


def test_error_from_level_is_structured_error_with_message():
    trace = from_langfuse_trace(
        {
            "id": "t",
            "observations": [
                _tool_obs(
                    "o1",
                    "issue_refund",
                    {"order_id": "Z"},
                    "boom",
                    level="ERROR",
                    status_message="order not found",
                )
            ],
        }
    )
    result = trace.tool_results()[0]
    assert result.status is ResultStatus.ERROR
    assert result.error == "order not found"


def test_error_from_structured_http_status():
    obs = _tool_obs(
        "o1", "lookup_order", {"order_id": "Z"}, {"http_status": 404, "detail": "missing"}
    )
    trace = from_langfuse_trace({"id": "t", "observations": [obs]})
    result = trace.tool_results()[0]
    assert result.status is ResultStatus.ERROR
    assert result.http_status == 404


def test_result_without_signals_is_unknown_not_guessed():
    obs = _tool_obs("o1", "lookup_order", {"order_id": "A100"}, {"amount": 49.99})
    trace = from_langfuse_trace({"id": "t", "observations": [obs]})
    assert trace.tool_results()[0].status is ResultStatus.UNKNOWN


def test_observe_kwargs_input_is_unwrapped():
    # Langfuse @observe records a wrapped function's inputs as {"args": [...], "kwargs": {...}}.
    obs = _tool_obs(
        "o1",
        "issue_refund",
        {"args": [], "kwargs": {"order_id": "A100", "amount": 49.99}},
        {"ok": True},
    )
    trace = from_langfuse_trace({"id": "t", "observations": [obs]})
    assert trace.tool_calls()[0].args == {"order_id": "A100", "amount": 49.99}


def test_json_string_input_is_parsed():
    obs = _tool_obs("o1", "lookup_order", '{"order_id": "A100"}', {"amount": 49.99})
    trace = from_langfuse_trace({"id": "t", "observations": [obs]})
    assert trace.tool_calls()[0].args == {"order_id": "A100"}


def test_generation_text_becomes_assistant_message():
    obs = {
        "id": "g1",
        "type": "generation",
        "output": {"content": "here is your refund"},
        "startTime": "2024-01-01T00:00:00Z",
    }
    trace = from_langfuse_trace({"id": "t", "observations": [obs]})
    msgs = [s for s in trace.steps if isinstance(s, Message) and s.role is Role.ASSISTANT]
    assert msgs and msgs[0].content == "here is your refund"


def test_openai_style_tool_calls_in_generation_fallback():
    # No tool/span observations → fall back to OpenAI-style tool_calls embedded in a generation.
    gen = {
        "id": "g1",
        "type": "generation",
        "output": {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "c1",
                    "function": {"name": "lookup_order", "arguments": '{"order_id": "A100"}'},
                }
            ],
        },
    }
    trace = from_langfuse_trace({"id": "t", "observations": [gen]})
    call = trace.tool_calls()[0]
    assert call.name == "lookup_order" and call.args == {"order_id": "A100"}


def test_no_double_count_when_tool_observation_present():
    # A generation lists a tool_call AND a tool observation records the execution: count once.
    gen = {
        "id": "g1",
        "type": "generation",
        "startTime": "2024-01-01T00:00:00Z",
        "output": {
            "tool_calls": [{"id": "c1", "function": {"name": "lookup_order", "arguments": "{}"}}]
        },
    }
    tool = _tool_obs(
        "o1", "lookup_order", {"order_id": "A100"}, {"amount": 1}, start="2024-01-01T00:00:01Z"
    )
    trace = from_langfuse_trace({"id": "t", "observations": [gen, tool]})
    assert len(trace.tool_calls()) == 1  # the tool observation wins; generation tool_calls ignored


def test_observations_ordered_by_start_time():
    a = _tool_obs("o2", "second", {}, {}, start="2024-01-01T00:00:02Z")
    b = _tool_obs("o1", "first", {}, {}, start="2024-01-01T00:00:01Z")
    trace = from_langfuse_trace({"id": "t", "observations": [a, b]})
    assert [c.name for c in trace.tool_calls()] == ["first", "second"]


def test_metadata_openinference_tool_kind_detected():
    obs = {
        "id": "o1",
        "type": "span",
        "name": "issue_refund",
        "input": {},
        "output": {},
        "metadata": {"openinference.span.kind": "TOOL"},
    }
    trace = from_langfuse_trace({"id": "t", "observations": [obs]})
    assert trace.tool_calls() and trace.tool_calls()[0].name == "issue_refund"


def test_accepts_sdk_object_with_model_dump():
    class _FakeTrace:
        def model_dump(self):
            return {
                "id": "t",
                "observations": [
                    _tool_obs("o1", "lookup_order", {"order_id": "A100"}, {"amount": 1})
                ],
            }

    trace = from_langfuse_trace(_FakeTrace())
    assert trace.tool_calls()[0].name == "lookup_order"


def test_observed_tool_names_lists_recognized_tools():
    obs = [
        _tool_obs("o1", "lookup_order", {}, {}),
        {"id": "o2", "type": "span", "name": "retrieval", "input": {}, "output": {}},
        {"id": "o3", "type": "span", "name": "issue_refund", "input": {}, "output": {}},
    ]
    trace = {"id": "t", "observations": obs}
    assert observed_tool_names(trace, tool_names=["issue_refund"]) == [
        "issue_refund",
        "lookup_order",
    ]


def test_real_v4_positional_args_and_snake_case_error():
    # Shapes verified against a real Langfuse v4 fetched trace: @observe records positional args
    # as {"args": [{...}], "kwargs": {}}, and the error text is snake_case `status_message`.
    obs = {
        "id": "o1",
        "type": "TOOL",
        "name": "lookup_order",
        "input": {"args": [{"order_id": "Z999"}], "kwargs": {}},
        "output": None,
        "level": "ERROR",
        "status_message": "order not found",
    }
    trace = from_langfuse_trace({"id": "t", "observations": [obs]})
    call = trace.tool_calls()[0]
    assert call.args == {"order_id": "Z999"}  # unwrapped from args[0], not the empty kwargs
    result = trace.result_for(call)
    assert result.status is ResultStatus.ERROR
    assert result.error == "order not found"  # read from snake_case status_message


def test_real_v4_observe_root_input_seeds_user_message():
    # The @observe root span's input is {"args": [task], "kwargs": {}}; the task must become the
    # user turn (so provenance/R3 sees what the user asked).
    trace = from_langfuse_trace(
        {
            "id": "t",
            "input": {"args": ["Refund order Z999."], "kwargs": {}},
            "observations": [],
        }
    )
    users = [s for s in trace.steps if isinstance(s, Message) and s.role is Role.USER]
    assert users and users[0].content == "Refund order Z999."


def test_end_to_end_schema_violation_from_langfuse_trace():
    registry = ToolRegistry.from_dict(
        {
            "tools": {
                "cancel_order": {
                    "schema": {
                        "type": "object",
                        "properties": {"order_id": {"type": "string"}},
                        "required": ["order_id"],
                    }
                }
            }
        }
    )
    # A planted violation: order_id emitted as an int by the model.
    trace = from_langfuse_trace(
        {
            "id": "t",
            "observations": [
                _tool_obs("o1", "cancel_order", {"order_id": 4521}, {"cancelled": True})
            ],
        }
    )
    report = lint_trace(trace, [SchemaViolationRule()], registry)
    assert report.exit_code == 2
    assert report.active_findings[0].evidence["errors"][0]["keyword"] == "type"
