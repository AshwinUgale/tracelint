"""The provider on-ramps: load_source(path, fmt) and the lint_* convenience wrappers.

These check the file-loading + format-dispatch layer only — the adapters themselves are covered by
test_adapter_*. The point here is that each ``--format`` reaches the right adapter, multi-trace
inputs fan out correctly, and a bad format fails loudly.
"""

from __future__ import annotations

import json

import pytest

from tracelint import lint_otel_trace, load_source
from tracelint.findings import ConfidenceTier
from tracelint.trace import ResultStatus


def _tool_span(span_id, start, name, args, output, *, trace_id="t1", error=False):
    span = {
        "span_id": span_id,
        "trace_id": trace_id,
        "start_time": start,
        "name": name,
        "status_code": "ERROR" if error else "OK",
        "attributes": {
            "openinference.span.kind": "TOOL",
            "tool.name": name,
            "input.value": json.dumps(args),
            "output.value": json.dumps(output),
        },
    }
    return span


def _write(tmp_path, obj, name="src.json"):
    p = tmp_path / name
    p.write_text(json.dumps(obj), encoding="utf-8")
    return str(p)


# --- OpenInference / OTel ------------------------------------------------------------


def test_otel_flat_list_is_one_trace(tmp_path):
    spans = [
        _tool_span("s1", "2024-01-01T00:00:01Z", "search", {"q": "x"}, {"hit": 1}),
        _tool_span("s2", "2024-01-01T00:00:02Z", "book", {"id": "1"}, {"ok": True}),
    ]
    traces = load_source(_write(tmp_path, spans), "openinference")
    assert len(traces) == 1
    assert [c.name for c in traces[0].tool_calls()] == ["search", "book"]


def test_otel_alias_and_error_status(tmp_path):
    spans = [
        _tool_span("s1", "2024-01-01T00:00:01Z", "charge", {"amt": 5}, {"error": "no"}, error=True)
    ]
    # "otel" is an alias for "openinference".
    traces = load_source(_write(tmp_path, spans), "otel")
    result = traces[0].tool_results()[0]
    assert result.status is ResultStatus.ERROR


def test_otel_groups_by_trace_id(tmp_path):
    spans = [
        _tool_span("s1", "2024-01-01T00:00:01Z", "a", {}, {}, trace_id="run-a"),
        _tool_span("s2", "2024-01-01T00:00:02Z", "b", {}, {}, trace_id="run-b"),
    ]
    traces = load_source(_write(tmp_path, spans), "openinference")
    # Two distinct trace ids → two separate traces.
    assert len(traces) == 2
    assert {t.run_id for t in traces} == {"run-a", "run-b"}


def test_otel_otlp_resource_spans_envelope(tmp_path):
    span = {
        "spanId": "s1",
        "traceId": "abc",
        "startTimeUnixNano": "1",
        "name": "lookup",
        "attributes": [
            {"key": "openinference.span.kind", "value": {"stringValue": "TOOL"}},
            {"key": "tool.name", "value": {"stringValue": "lookup"}},
            {"key": "input.value", "value": {"stringValue": json.dumps({"id": 1})}},
            {"key": "output.value", "value": {"stringValue": json.dumps({"ok": True})}},
        ],
    }
    doc = {"resourceSpans": [{"scopeSpans": [{"spans": [span]}]}]}
    traces = load_source(_write(tmp_path, doc), "openinference")
    assert len(traces) == 1
    assert traces[0].tool_calls()[0].name == "lookup"


def test_otel_jsonl_one_trace_per_line(tmp_path):
    line1 = [_tool_span("s1", "2024-01-01T00:00:01Z", "a", {}, {}, trace_id="r1")]
    line2 = [_tool_span("s2", "2024-01-01T00:00:01Z", "b", {}, {}, trace_id="r2")]
    p = tmp_path / "many.jsonl"
    p.write_text(json.dumps(line1) + "\n" + json.dumps(line2) + "\n", encoding="utf-8")
    traces = load_source(str(p), "openinference")
    assert len(traces) == 2


# --- OpenAI --------------------------------------------------------------------------


def test_openai_message_list(tmp_path):
    messages = [
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "c1", "function": {"name": "get", "arguments": json.dumps({"x": 1})}}
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "ok", "status": "ok"},
    ]
    traces = load_source(_write(tmp_path, messages), "openai")
    assert len(traces) == 1
    assert traces[0].tool_calls()[0].name == "get"


def test_openai_messages_object_wrapper(tmp_path):
    doc = {"run_id": "r9", "messages": [{"role": "user", "content": "hi"}]}
    traces = load_source(_write(tmp_path, doc), "openai")
    assert traces[0].run_id == "r9"


# --- Langfuse ------------------------------------------------------------------------


def test_langfuse_single_and_list(tmp_path):
    trace = {
        "id": "lf1",
        "observations": [
            {"id": "o1", "type": "tool", "name": "search", "input": {"q": "x"}, "output": {"n": 1}}
        ],
    }
    one = load_source(_write(tmp_path, trace, "one.json"), "langfuse")
    assert len(one) == 1 and one[0].run_id == "lf1"

    many = load_source(_write(tmp_path, [trace, trace], "many.json"), "langfuse")
    assert len(many) == 2


# --- Errors + convenience wrapper ----------------------------------------------------


def test_unknown_format_raises(tmp_path):
    with pytest.raises(ValueError, match="unknown --format"):
        load_source(_write(tmp_path, []), "nope")


def test_native_format_unchanged(tmp_path):
    from tracelint.trace import Trace

    native = Trace(run_id="n1", steps=[]).to_dict()
    traces = load_source(_write(tmp_path, native), "native")
    assert len(traces) == 1 and traces[0].run_id == "n1"


def test_lint_otel_trace_flags_tool_error():
    spans = [
        _tool_span("s1", "2024-01-01T00:00:01Z", "charge", {"amt": 5}, {"error": "no"}, error=True)
    ]
    report = lint_otel_trace(spans)
    events = report.by_tier(ConfidenceTier.HARD_EVENT)
    assert any(f.rule == "R2a" for f in events)
