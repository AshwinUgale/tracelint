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


def test_phoenix_export_top_level_span_kind_is_recognized():
    """Arize Phoenix's own trace export puts the kind in a top-level ``span_kind`` field (not the
    ``openinference.span.kind`` attribute). Regression: a real Phoenix export must be recognized,
    not silently reduced to zero tool calls. Derived from a real Arize-ai/phoenix fixture."""
    spans = [
        {
            "context": {"trace_id": "fixture-trace-1", "span_id": "s1"},
            "name": "list_datasets",
            "span_kind": "TOOL",  # top-level, the Phoenix export shape
            "start_time": "2024-01-01T00:00:01Z",
            "status_code": "OK",
            "attributes": {"tool.name": "list_datasets", "input.value": "{}", "output.value": "[]"},
        },
        {
            "context": {"trace_id": "fixture-trace-1", "span_id": "s2"},
            "name": "add_spans",
            "span_kind": "TOOL",
            "start_time": "2024-01-01T00:00:02Z",
            "status_code": "ERROR",
            "status_message": "GraphQL error",
            "attributes": {"tool.name": "add_spans", "input.value": "{}", "output.value": "{}"},
        },
    ]
    trace = from_otel_spans(spans)
    assert [c.name for c in trace.tool_calls()] == ["list_datasets", "add_spans"]
    assert trace.run_id == "fixture-trace-1"
    # The errored Phoenix span is still read as a structured error (R2a's hard-event signal).
    err = trace.tool_results()[1]
    assert err.status is ResultStatus.ERROR
    report = lint_trace(trace, default_rules())
    assert any(f.rule == "R2a" for f in report.by_tier(ConfidenceTier.HARD_EVENT))


def test_error_from_nested_otel_status_shapes():
    """A real OTel export nests the status: the SDK's ReadableSpan.to_json emits
    ``{"status": {"status_code": "ERROR"}}`` and OTLP-JSON emits ``{"code": "STATUS_CODE_ERROR"}``
    or the numeric ``2``. Regression: read the status from those shapes, not only a flat
    ``status_code`` — a real SDK-exported ERROR was missed when its output payload was clean."""
    def tool(status):
        return {
            "span_id": "t1",
            "name": "charge",
            "start_time": "1",
            "status": status,
            "attributes": {
                "openinference.span.kind": "TOOL",
                "tool.name": "charge",
                "input.value": "{}",
                "output.value": '{"receipt": "ok"}',  # clean payload: only the status signals error
            },
        }
    shapes = (
        {"status_code": "ERROR", "description": "boom"},  # OTel SDK ReadableSpan.to_json
        {"code": "STATUS_CODE_ERROR"},  # OTLP-JSON string code
        {"code": 2},  # OTLP numeric ERROR
    )
    for status in shapes:
        result = from_otel_spans([tool(status)]).tool_results()[0]
        assert result.status is ResultStatus.ERROR, status


def test_input_messages_seed_user_turn_for_provenance():
    """OpenInference records the user's request under llm.input_messages. Seeding it (from the
    first LLM span only) gives provenance something to check against, so an argument the user
    actually supplied is not falsely flagged as a hallucinated/underivable value (R3)."""
    spans = [
        {
            "span_id": "l1",
            "name": "llm",
            "start_time": "1",
            "attributes": {
                "openinference.span.kind": "LLM",
                "llm.input_messages.0.message.role": "user",
                "llm.input_messages.0.message.content": "Cancel order A100.",
                "llm.output_messages.0.message.role": "assistant",
                "llm.output_messages.0.message.content": "Cancelling.",
            },
        },
        {
            "span_id": "t1",
            "name": "cancel_order",
            "start_time": "2",
            "status_code": "OK",
            "attributes": {
                "openinference.span.kind": "TOOL",
                "tool.name": "cancel_order",
                "input.value": '{"order_id": "A100"}',
                "output.value": '{"ok": true}',
            },
        },
    ]
    trace = from_otel_spans(spans)
    assert any(m.role.value == "user" and "A100" in m.content for m in trace.messages())
    report = lint_trace(trace, default_rules())
    # 'A100' came from the user turn, so R3 must not flag it as underivable.
    assert not [f for f in report.active_findings if f.rule == "R3"]


def test_python_repr_tool_args_are_parsed_not_malformed():
    """Some instrumentations serialize a tool's arguments with str(dict)/repr (single-quoted keys),
    which isn't valid JSON but is a well-formed argument object — not a malformed call. Regression
    (found on real Phoenix agent traces): parse via literal_eval so R6 doesn't fire a false
    hard_defect on every such call."""
    span = {
        "span_id": "t1",
        "name": "product_search",
        "start_time": "1",
        "status_code": "OK",
        "attributes": {
            "openinference.span.kind": "TOOL",
            "tool.name": "product_search",
            "input.value": "{'query': 'tablet', 'page_size': 5, 'in_stock': True}",
            "output.value": "{}",
        },
    }
    trace = from_otel_spans([span])
    call = trace.tool_calls()[0]
    assert call.args == {"query": "tablet", "page_size": 5, "in_stock": True}
    assert call.raw_text is None
    report = lint_trace(trace, default_rules())
    assert not report.has_hard_defect
    assert not [f for f in report.active_findings if f.rule == "R6"]


def test_events_as_nonlist_does_not_crash_on_truthiness():
    """Phoenix get_spans_dataframe records store ``events`` as a numpy array; ``array or []`` raises
    'truth value of an array is ambiguous'. Regression: never boolean-test the events value."""

    class _AmbiguousTruth(list):
        def __bool__(self):
            raise ValueError("truth value is ambiguous")

    span = {
        "span_id": "t1",
        "name": "charge",
        "start_time": "1",
        "status_code": "OK",
        "events": _AmbiguousTruth([{"name": "other"}]),
        "attributes": {
            "openinference.span.kind": "TOOL",
            "tool.name": "charge",
            "input.value": "{}",
            "output.value": "{}",
        },
    }
    trace = from_otel_spans([span])  # must not raise
    assert trace.tool_calls()[0].name == "charge"


def test_phoenix_dataframe_record_shape_is_recognized():
    """The path a real Phoenix user takes — ``px.Client().get_spans_dataframe().to_dict("records")``
    — yields flat rows: required columns at top level and attributes as ``attributes.*`` columns
    (Phoenix's ATTRIBUTE_PREFIX). Regression: read args/output from those prefixed columns, not
    just a nested ``attributes`` dict, so a record span isn't recognized with empty args."""
    records = [
        {
            "name": "lookup_order",
            "span_kind": "TOOL",
            "start_time": "2024-01-01T00:00:01Z",
            "status_code": "OK",
            "context.span_id": "s1",
            "context.trace_id": "df-trace-1",
            "attributes.tool.name": "lookup_order",
            "attributes.input.value": '{"order_id": "A100"}',
            "attributes.output.value": '{"amount": 49.99}',
        },
        {
            "name": "charge_card",
            "span_kind": "TOOL",
            "start_time": "2024-01-01T00:00:02Z",
            "status_code": "ERROR",
            "status_message": "gateway 402",
            "context.span_id": "s2",
            "context.trace_id": "df-trace-1",
            "attributes.tool.name": "charge_card",
            "attributes.input.value": '{"amount": 49.99}',
            "attributes.output.value": '{"error": "declined"}',
        },
    ]
    trace = from_otel_spans(records)
    call = trace.tool_calls()[0]
    assert call.name == "lookup_order"
    assert call.args == {"order_id": "A100"}  # read from the "attributes.input.value" column
    assert trace.result_for(call).content == {"amount": 49.99}
    assert trace.run_id == "df-trace-1"
    assert trace.tool_results()[1].status is ResultStatus.ERROR


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
