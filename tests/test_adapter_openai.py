"""Phase 1a — the OpenAI chat-completions adapter (spec §II.4)."""

from __future__ import annotations

from tracelint import (
    SchemaViolationRule,
    from_openai_messages,
    lint_trace,
    openai_tools_to_registry,
)
from tracelint.trace import Message, ResultStatus, ToolCall, ToolResult


def _assistant_call(call_id: str, name: str, arguments: str) -> dict:
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}
        ],
    }


def test_sharegpt_from_value_shape_is_read():
    # ShareGPT uses `from`/`value` and human/gpt role names instead of role/content.
    trace = from_openai_messages(
        [
            {"from": "human", "value": "find shoes"},
            {
                "from": "gpt",
                "value": "searching",
                "tool_calls": [{"id": "c1", "function": {"name": "search", "arguments": "{}"}}],
            },
        ]
    )
    assert [m.role.value for m in trace.messages()] == ["user", "assistant"]
    assert trace.tool_calls()[0].name == "search"


def test_typed_block_content_is_flattened_to_text():
    # Anthropic / newer-OpenAI content is a list of typed blocks; flatten it for messages + tool
    # results (this is the SWE-bench shape that previously needed a hand transform).
    trace = from_openai_messages(
        [
            {"role": "user", "content": [{"type": "text", "text": "fix the bug"}]},
            {"role": "tool", "tool_call_id": "c1", "content": [{"type": "text", "text": "boom"}]},
        ]
    )
    assert trace.messages()[0].content == "fix the bug"
    assert trace.tool_results()[0].content == "boom"


def test_role_plus_text_shape_is_read():
    trace = from_openai_messages([{"role": "user", "text": "cancel A100"}])
    assert trace.messages()[0].content == "cancel A100"


def test_structured_tool_result_dict_is_preserved_not_flattened():
    # A structured tool result (a dict, or a data list) must stay intact so R2 / failure_when can
    # read it — only *typed-block* content is flattened to text.
    trace = from_openai_messages(
        [{"role": "tool", "tool_call_id": "c1", "content": {"status": "declined"}}]
    )
    assert trace.tool_results()[0].content == {"status": "declined"}
    data = from_openai_messages(
        [{"role": "tool", "tool_call_id": "c2", "content": [{"id": 1}, {"id": 2}]}]
    )
    assert data.tool_results()[0].content == [{"id": 1}, {"id": 2}]


def test_arguments_json_string_is_parsed_into_args():
    trace = from_openai_messages(
        [
            {"role": "user", "content": "weather?"},
            _assistant_call("call_1", "get_weather", '{"city": "Austin", "units": "f"}'),
            {"role": "tool", "tool_call_id": "call_1", "content": '{"tempF": 78}'},
        ]
    )
    call = trace.tool_calls()[0]
    assert isinstance(call, ToolCall)
    assert call.args == {"city": "Austin", "units": "f"}
    assert call.raw_text == '{"city": "Austin", "units": "f"}'


def test_malformed_arguments_keep_raw_text_and_empty_args():
    trace = from_openai_messages([_assistant_call("c1", "t", '{"city": "Austin"')])  # truncated
    call = trace.tool_calls()[0]
    assert call.args == {}
    assert call.raw_text == '{"city": "Austin"'


def test_tool_result_paired_and_error_signals_carried():
    trace = from_openai_messages(
        [
            _assistant_call("c1", "reserve", "{}"),
            {"role": "tool", "tool_call_id": "c1", "content": "boom", "http_status": 500},
        ]
    )
    call = trace.tool_calls()[0]
    result = trace.result_for(call)
    assert isinstance(result, ToolResult)
    assert result.status is ResultStatus.ERROR  # http_status >= 400
    assert result.http_status == 500


def test_explicit_status_field_wins():
    trace = from_openai_messages(
        [
            _assistant_call("c1", "t", "{}"),
            {"role": "tool", "tool_call_id": "c1", "content": "{}", "status": "ok"},
        ]
    )
    assert trace.tool_results()[0].status is ResultStatus.OK


def test_result_without_signals_is_unknown_not_guessed():
    trace = from_openai_messages(
        [
            _assistant_call("c1", "t", "{}"),
            {"role": "tool", "tool_call_id": "c1", "content": "anything"},
        ]
    )
    # The wire format does not say ok/error; the adapter stays faithful (R2a decides later).
    assert trace.tool_results()[0].status is ResultStatus.UNKNOWN


def test_assistant_content_and_tool_calls_both_captured():
    trace = from_openai_messages(
        [
            {
                "role": "assistant",
                "content": "let me check",
                "tool_calls": [
                    {"id": "c1", "type": "function", "function": {"name": "t", "arguments": "{}"}}
                ],
            }
        ]
    )
    assert isinstance(trace.steps[0], Message)
    assert isinstance(trace.steps[1], ToolCall)


def test_final_defaults_to_last_assistant_text():
    trace = from_openai_messages(
        [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "the answer is 42"},
        ]
    )
    assert trace.final == "the answer is 42"


def test_tools_to_registry_and_end_to_end_lint():
    tools = [
        {
            "type": "function",
            "function": {
                "name": "cancel_order",
                "parameters": {
                    "type": "object",
                    "properties": {"order_id": {"type": "string"}},
                    "required": ["order_id"],
                },
            },
        }
    ]
    registry = openai_tools_to_registry(tools)
    assert registry.schema_for("cancel_order")["required"] == ["order_id"]

    # A planted violation: order_id emitted as an int.
    trace = from_openai_messages([_assistant_call("c1", "cancel_order", '{"order_id": 4521}')])
    report = lint_trace(trace, [SchemaViolationRule()], registry)
    assert report.exit_code == 2
    assert report.active_findings[0].evidence["errors"][0]["keyword"] == "type"
