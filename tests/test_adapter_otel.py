"""OpenTelemetry / OpenInference adapter."""

from __future__ import annotations

from tracelint import (
    ConfidenceTier,
    ToolRegistry,
    default_rules,
    from_otel_spans,
    lint_trace,
)
from tracelint.trace import Message, ResultStatus, ToolCall, ToolResult


def _tool_span(sid, name, inp, out, *, status="OK", status_message=None, start="1"):
    return {
        "span_id": sid,
        "name": name,
        "start_time": start,
        "status_code": status,
        "status_message": status_message,
        "attributes": {
            "openinference.span.kind": "TOOL",
            "tool.name": name,
            "input.value": inp,
            "output.value": out,
        },
    }


def test_tool_span_becomes_paired_call_and_result():
    trace = from_otel_spans(
        [_tool_span("s1", "lookup_order", '{"order_id": "A100"}', '{"amount": 49.99}')]
    )
    call = trace.tool_calls()[0]
    assert isinstance(call, ToolCall) and call.name == "lookup_order"
    assert call.args == {"order_id": "A100"}  # input.value JSON string parsed
    result = trace.result_for(call)
    assert isinstance(result, ToolResult) and result.content == {"amount": 49.99}
    assert result.status is ResultStatus.UNKNOWN  # no error signal -> faithful


def test_error_from_otel_status_code():
    span = _tool_span(
        "s1",
        "lookup_order",
        '{"order_id": "Z"}',
        "boom",
        status="ERROR",
        status_message="order not found",
    )
    result = from_otel_spans([span]).tool_results()[0]
    assert result.status is ResultStatus.ERROR
    assert result.error == "order not found"


def test_error_from_exception_event():
    span = _tool_span("s1", "issue_refund", "{}", "null")
    span["events"] = [
        {"name": "exception", "attributes": {"exception.message": "refund failed: 500"}}
    ]
    result = from_otel_spans([span]).tool_results()[0]
    assert result.status is ResultStatus.ERROR
    assert "refund failed" in (result.error or "")


def test_error_from_structured_output_http_status():
    span = _tool_span("s1", "lookup_order", "{}", {"http_status": 404, "detail": "missing"})
    result = from_otel_spans([span]).tool_results()[0]
    assert result.status is ResultStatus.ERROR


def test_otlp_attribute_list_form_is_normalized():
    # Raw OTLP: attributes as a typed key/value list, not a flat dict.
    span = {
        "spanId": "s1",
        "name": "lookup_order",
        "startTimeUnixNano": "1000",
        "status": {"code": "OK"},
        "attributes": [
            {"key": "openinference.span.kind", "value": {"stringValue": "TOOL"}},
            {"key": "tool.name", "value": {"stringValue": "lookup_order"}},
            {"key": "input.value", "value": {"stringValue": '{"order_id": "A100"}'}},
            {"key": "output.value", "value": {"stringValue": '{"amount": 1}'}},
        ],
    }
    call = from_otel_spans([span]).tool_calls()[0]
    assert call.name == "lookup_order" and call.args == {"order_id": "A100"}


def test_llm_span_tool_calls_reconstructed_from_dotted_keys():
    # No TOOL span -> fall back to OpenAI-style tool_calls flattened on an LLM span.
    span = {
        "span_id": "s1",
        "name": "llm",
        "start_time": "1",
        "attributes": {
            "openinference.span.kind": "LLM",
            "llm.output_messages.0.message.role": "assistant",
            "llm.output_messages.0.message.tool_calls.0.tool_call.id": "c1",
            "llm.output_messages.0.message.tool_calls.0.tool_call.function.name": "lookup_order",
            "llm.output_messages.0.message.tool_calls.0.tool_call.function.arguments": (
                '{"order_id": "A100"}'
            ),
        },
    }
    call = from_otel_spans([span]).tool_calls()[0]
    assert call.name == "lookup_order" and call.args == {"order_id": "A100"}


def test_llm_text_becomes_assistant_message_and_final():
    span = {
        "span_id": "s1",
        "name": "llm",
        "start_time": "1",
        "attributes": {
            "openinference.span.kind": "LLM",
            "llm.output_messages.0.message.role": "assistant",
            "llm.output_messages.0.message.content": "your refund is processed",
        },
    }
    trace = from_otel_spans([span])
    msgs = [s for s in trace.steps if isinstance(s, Message)]
    assert msgs and msgs[0].content == "your refund is processed"
    assert trace.final == "your refund is processed"


def test_spans_ordered_by_start_time():
    a = _tool_span("s2", "second", "{}", "{}", start="200")
    b = _tool_span("s1", "first", "{}", "{}", start="100")
    trace = from_otel_spans([a, b])
    assert [c.name for c in trace.tool_calls()] == ["first", "second"]


def test_non_tool_spans_are_skipped():
    span = {
        "span_id": "s1",
        "name": "retrieval",
        "start_time": "1",
        "attributes": {"openinference.span.kind": "RETRIEVER", "input.value": "q"},
    }
    assert from_otel_spans([span]).tool_calls() == []


def test_trail_patronus_envelope_real_shape():
    # Verified against a real TRAIL (Patronus/smolagents) trace: attributes live under
    # `span_attributes`, spans nest via `child_spans`, the name is `span_name`, args are wrapped
    # as {"args": [], "kwargs": {...}}, and status_code is "Error".
    root = {
        "span_id": "root",
        "span_name": "main",
        "timestamp": "2025-03-19T16:48:04Z",
        "span_kind": "Internal",
        "span_attributes": {},
        "child_spans": [
            {
                "span_id": "tool1",
                "span_name": "TextInspectorTool",
                "timestamp": "2025-03-19T16:48:05Z",
                "status_code": "Error",
                "status_message": "FileConversionException: Could not convert 'words_alpha.txt'",
                "span_attributes": {
                    "openinference.span.kind": "TOOL",
                    "tool.name": "inspect_file_as_text",
                    "input.value": (
                        '{"args": [], "sanitize_inputs_outputs": false, '
                        '"kwargs": {"file_path": "words_alpha.txt", "question": ""}}'
                    ),
                },
            }
        ],
    }
    trace = from_otel_spans([root])
    call = trace.tool_calls()[0]
    assert call.name == "inspect_file_as_text"
    assert call.args == {"file_path": "words_alpha.txt", "question": ""}  # unwrapped from kwargs
    result = trace.result_for(call)
    assert result.status is ResultStatus.ERROR
    assert "FileConversionException" in (result.error or "")
    report = lint_trace(trace, default_rules(), ToolRegistry())
    assert any(f.rule == "R2a" for f in report.active_findings)  # real tool error localized


def test_end_to_end_lint_from_otel():
    registry = ToolRegistry.from_dict(
        {
            "tools": {
                "issue_refund": {
                    "schema": {
                        "type": "object",
                        "properties": {
                            "order_id": {"type": "string"},
                            "amount": {"type": "number"},
                        },
                        "required": ["order_id", "amount"],
                    }
                }
            }
        }
    )
    # amount as a string violates the schema -> R1 hard_defect from an OTel trace.
    span = _tool_span(
        "s1", "issue_refund", '{"order_id": "A100", "amount": "fifty"}', '{"ok": true}'
    )
    report = lint_trace(from_otel_spans([span]), default_rules(), registry)
    assert report.exit_code == 2
    assert any(
        f.rule == "R1" and f.tier is ConfidenceTier.HARD_DEFECT for f in report.active_findings
    )
