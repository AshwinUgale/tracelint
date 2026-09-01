"""Conformance: the OTel/OpenInference event-list reader (Phoenix span_kind + GenAI semconv)."""

from __future__ import annotations

import json

from conformance._harness import check
from tracelint.adapters.otel import from_otel_spans


def test_openinference_phoenix_top_level_span_kind():
    # Phoenix's own export puts the kind in a top-level ``span_kind`` (not the attribute).
    raw = [
        {
            "context": {"trace_id": "t", "span_id": "s1"},
            "name": "get_order",
            "span_kind": "TOOL",
            "start_time": "1",
            "status_code": "OK",
            "attributes": {
                "tool.name": "get_order",
                "input.value": '{"id": "A1"}',
                "output.value": '{"status": "ok"}',
            },
        },
        {
            "context": {"trace_id": "t", "span_id": "s2"},
            "name": "charge",
            "span_kind": "TOOL",
            "start_time": "2",
            "status_code": "ERROR",
            "status_message": "boom",
            "attributes": {
                "tool.name": "charge",
                "input.value": '{"amt": 5}',
                "output.value": "",
            },
        },
    ]
    check(
        from_otel_spans(raw),
        [
            ("tool_call", "get_order", {"id": "A1"}),
            # A span completing OK does not prove the tool's domain result succeeded → UNKNOWN.
            ("tool_result", "get_order", "unknown", None, None, {"status": "ok"}),
            ("tool_call", "charge", {"amt": 5}),
            ("tool_result", "charge", "error", None, "boom", ""),
        ],
    )


def test_genai_semconv_execute_tool():
    # OTel GenAI: gen_ai.operation.name == 'execute_tool', gen_ai.tool.name, plain input/output.
    raw = [
        {
            "span_id": "s1",
            "name": "get_weather",
            "start_time": "1",
            "status_code": "OK",
            "attributes": {
                "gen_ai.operation.name": "execute_tool",
                "gen_ai.tool.name": "get_weather",
                "gen_ai.tool.call.id": "1234",
                "input": json.dumps({"city": "Paris"}),
                "output": json.dumps({"report": "rainy"}),
            },
        },
        {
            "span_id": "s2",
            "name": "charge",
            "start_time": "2",
            "status": {"status_code": "ERROR"},  # nested OTel status shape
            "attributes": {
                "gen_ai.operation.name": "execute_tool",
                "gen_ai.tool.name": "charge",
                "input": json.dumps({"amount": 5}),
                "output": json.dumps({"error": "declined"}),
            },
        },
    ]
    check(
        from_otel_spans(raw),
        [
            ("tool_call", "get_weather", {"city": "Paris"}),
            ("tool_result", "get_weather", "unknown", None, None, {"report": "rainy"}),
            ("tool_call", "charge", {"amount": 5}),
            ("tool_result", "charge", "error", None, None, {"error": "declined"}),
        ],
    )
