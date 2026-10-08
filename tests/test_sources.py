"""The provider on-ramps: load_source(path, fmt) and the lint_* convenience wrappers.

These check the file-loading + format-dispatch layer only — the adapters themselves are covered by
test_adapter_*. The point here is that each ``--format`` reaches the right adapter, multi-trace
inputs fan out correctly, and a bad format fails loudly.
"""

from __future__ import annotations

import json

import pytest

from tracelint import lint_langsmith_trace, lint_otel_trace, load_source
from tracelint.findings import ConfidenceTier
from tracelint.sources import load_sources
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


def _write_jsonl(tmp_path, lines, name="spans.jsonl"):
    p = tmp_path / name
    p.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
    return str(p)


def _calls(trace):
    return [c.name for c in trace.tool_calls()]


A1 = _tool_span("a1", "2024-01-01T00:00:01Z", "search", {"q": "x"}, {"hit": 1}, trace_id="run-a")
B1 = _tool_span("b1", "2024-01-01T00:00:02Z", "lookup", {"id": "1"}, {"ok": True}, trace_id="run-b")
A2 = _tool_span("a2", "2024-01-01T00:00:03Z", "book", {"id": "1"}, {"ok": True}, trace_id="run-a")


def test_otel_jsonl_one_span_per_line_is_regrouped_into_runs(tmp_path):
    # Exporters write one span per line (Phoenix's to_json(lines=True), OTel file exporters), and
    # runs interleave. 0.9 linted each line as its own one-span trace, so no defect spanning two
    # steps could ever be seen.
    traces = load_source(_write_jsonl(tmp_path, [A1, B1, A2]), "openinference")
    assert [(t.run_id, _calls(t)) for t in traces] == [
        ("run-a", ["search", "book"]),
        ("run-b", ["lookup"]),
    ]


def _otlp_span(span_id, trace_id, start, name):
    attrs = {"openinference.span.kind": "TOOL", "tool.name": name, "input.value": "{}"}
    return {
        "spanId": span_id,
        "traceId": trace_id,
        "startTimeUnixNano": str(start),
        "name": name,
        "attributes": [{"key": k, "value": {"stringValue": v}} for k, v in attrs.items()],
    }


def test_otel_jsonl_otlp_batches_are_regrouped_into_runs(tmp_path):
    # An OTel collector's file exporter writes one export batch per line: a batch can mix runs, and
    # a run can span batches.
    lines = [
        {
            "resourceSpans": [
                {
                    "scopeSpans": [
                        {
                            "spans": [
                                _otlp_span("a1", "aa", 1, "search"),
                                _otlp_span("b1", "bb", 2, "lookup"),
                            ]
                        }
                    ]
                }
            ]
        },
        {"resourceSpans": [{"scopeSpans": [{"spans": [_otlp_span("a2", "aa", 3, "book")]}]}]},
    ]
    traces = load_source(_write_jsonl(tmp_path, lines), "openinference")
    assert [_calls(t) for t in traces] == [["search", "book"], ["lookup"]]


def _without_trace_id(span):
    return {k: v for k, v in span.items() if k != "trace_id"}


def test_otel_jsonl_spans_without_trace_ids_are_one_run(tmp_path):
    # One span per line and no ids: the file is one run, exactly like a JSON array of those spans.
    lines = [_without_trace_id(A1), _without_trace_id(A2)]
    traces = load_source(_write_jsonl(tmp_path, lines), "openinference")
    assert [_calls(t) for t in traces] == [["search", "book"]]


def test_otel_jsonl_whole_runs_without_trace_ids_stay_one_per_line(tmp_path):
    # Nothing to regroup by, and each line holds a whole run: merging them would invent repeats.
    lines = [[_without_trace_id(A1), _without_trace_id(A2)], [_without_trace_id(B1)]]
    traces = load_source(_write_jsonl(tmp_path, lines), "openinference")
    assert [_calls(t) for t in traces] == [["search", "book"], ["lookup"]]


def test_a_run_split_across_files_is_merged(tmp_path):
    # A rotating exporter, or a glob over batch files, can split one run across files.
    first = _write_jsonl(tmp_path, [A1], "part-1.jsonl")
    second = _write_jsonl(tmp_path, [A2, B1], "part-2.jsonl")
    loaded = load_sources([first, second], "openinference")
    assert [(t.run_id, _calls(t), path) for t, path in loaded] == [
        ("run-a", ["search", "book"], first),
        ("run-b", ["lookup"], second),
    ]


def test_the_same_file_twice_does_not_duplicate_calls(tmp_path):
    path = _write_jsonl(tmp_path, [A1, A2])
    loaded = load_sources([path, path], "openinference")
    assert [_calls(t) for t, _ in loaded] == [["search", "book"]]


def test_other_formats_keep_one_report_per_file_trace(tmp_path):
    run = {"id": "lf1", "observations": [{"id": "o1", "type": "tool", "name": "search"}]}
    path = _write(tmp_path, run)
    assert [p for _, p in load_sources([path, path], "langfuse")] == [path, path]


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


def test_openai_jsonl_one_message_per_line_is_one_conversation(tmp_path):
    # The docs promise "one message per line"; 0.9 made each message its own trace, so the tool
    # result never met its call.
    messages = [
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "c1", "function": {"name": "get", "arguments": json.dumps({"x": 1})}}
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "ok"},
    ]
    (trace,) = load_source(_write_jsonl(tmp_path, messages, "chat.jsonl"), "openai")
    call = trace.tool_calls()[0]
    assert trace.result_for(call).content == "ok"


def test_openai_jsonl_one_conversation_per_line(tmp_path):
    # The fine-tuning / batch format: one {"messages": [...]} per line, one trace each.
    lines = [{"messages": [{"role": "user", "content": n}]} for n in ("a", "b")]
    assert len(load_source(_write_jsonl(tmp_path, lines, "chats.jsonl"), "openai")) == 2


def test_sharegpt_datasets_are_read_not_skipped(tmp_path):
    # ShareGPT wraps each conversation as {"conversations": [...]} with `from`/`value` messages. The
    # adapter reads them, but 0.9's loader recognized neither the wrapper nor `from`, so it loaded
    # zero traces and exited 0 with no output at all.
    conv = [{"from": "human", "value": "find shoes"}, {"from": "gpt", "value": "Found 3."}]
    dataset = load_source(_write(tmp_path, [{"conversations": conv}] * 2), "openai")
    per_line = load_source(_write_jsonl(tmp_path, [{"conversations": conv}] * 2), "openai")
    per_message = load_source(_write_jsonl(tmp_path, conv, "messages.jsonl"), "openai")
    assert [len(dataset), len(per_line), len(per_message)] == [2, 2, 1]
    assert all(trace.steps for trace in dataset + per_line + per_message)


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


def test_langsmith_single_and_list(tmp_path):
    run = {
        "id": "ls1",
        "run_type": "chain",
        "child_runs": [
            {
                "id": "tool-1",
                "run_type": "tool",
                "name": "search",
                "inputs": {"q": "x"},
                "outputs": {"status": "ok"},
            }
        ],
    }
    one = load_source(_write(tmp_path, run, "one.json"), "langsmith")
    assert len(one) == 1 and one[0].run_id == "ls1"

    many = load_source(_write(tmp_path, [run, run], "many.json"), "langsmith")
    assert len(many) == 2


# --- Errors + convenience wrapper ----------------------------------------------------


def test_unknown_format_raises(tmp_path):
    with pytest.raises(ValueError, match="unknown --format"):
        load_source(_write(tmp_path, []), "nope")


def test_native_format_unchanged(tmp_path):
    from tracelint.trace import Message, Role, Trace

    native = Trace(run_id="n1", steps=[Message(Role.USER, "hi")]).to_dict()
    traces = load_source(_write(tmp_path, native), "native")
    assert len(traces) == 1 and traces[0].run_id == "n1"


def test_lint_otel_trace_flags_tool_error():
    spans = [
        _tool_span("s1", "2024-01-01T00:00:01Z", "charge", {"amt": 5}, {"error": "no"}, error=True)
    ]
    report = lint_otel_trace(spans)
    events = report.by_tier(ConfidenceTier.HARD_EVENT)
    assert any(f.rule == "R2a" for f in events)


def test_lint_langsmith_trace_flags_tool_error():
    report = lint_langsmith_trace(
        {
            "id": "ls1",
            "run_type": "tool",
            "name": "search",
            "inputs": {"q": "x"},
            "outputs": {"error": "no"},
        }
    )
    events = report.by_tier(ConfidenceTier.HARD_EVENT)
    assert any(f.rule == "R2a" for f in events)


# --- Nothing to lint is an input error, never a clean pass ---------------------------

SPANS = [
    _tool_span("s1", "2024-01-01T00:00:01Z", "search", {"q": "x"}, {"hit": 1}),
    _tool_span("s2", "2024-01-01T00:00:02Z", "book", {"id": "1"}, {"ok": True}),
]
OPENAI_MESSAGES = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]
LANGFUSE_TRACE = {"id": "lf1", "observations": [{"id": "o1", "type": "tool", "name": "search"}]}
LANGSMITH_RUN = {"id": "r1", "run_type": "chain", "child_runs": []}
NATIVE_TRACE = {"run_id": "n1", "steps": [{"type": "message", "role": "user", "content": "hi"}]}


def test_span_file_read_as_native_names_the_right_format(tmp_path):
    # The CLI's (and the GitHub Action's) default format is native. 0.9 read each span as an empty
    # native "trace" and exited 0 having checked nothing.
    with pytest.raises(ValueError, match="OpenInference / OTel spans: use --format openinference"):
        load_source(_write(tmp_path, SPANS), "native")


def test_span_per_line_file_read_as_native_names_the_line(tmp_path):
    path = _write_jsonl(tmp_path, SPANS)
    with pytest.raises(ValueError, match=r"line 1: not readable as native tracelint traces"):
        load_source(path, "native")


@pytest.mark.parametrize(
    ("doc", "fmt", "hint"),
    [
        (OPENAI_MESSAGES, "native", "use --format openai"),
        (LANGFUSE_TRACE, "native", "use --format langfuse"),
        (LANGSMITH_RUN, "native", "use --format langsmith"),
        ({"resourceSpans": []}, "native", "use --format openinference"),
        (NATIVE_TRACE, "openinference", "omit --format (native is the default)"),
        (NATIVE_TRACE, "openai", "omit --format (native is the default)"),
        (SPANS, "openai", "use --format openinference"),
        (SPANS, "langfuse", "use --format openinference"),
        (LANGFUSE_TRACE, "openinference", "use --format langfuse"),
        (LANGSMITH_RUN, "langfuse", "use --format langsmith"),
        (OPENAI_MESSAGES, "langsmith", "use --format openai"),
    ],
)
def test_wrong_format_is_an_input_error_that_names_the_right_one(tmp_path, doc, fmt, hint):
    with pytest.raises(ValueError, match=hint.replace("(", r"\(").replace(")", r"\)")):
        load_source(_write(tmp_path, doc), fmt)


@pytest.mark.parametrize(
    "fmt", ["native", "openinference", "openai", "langfuse", "langsmith", "atif"]
)
@pytest.mark.parametrize(("name", "content"), [("e.json", "[]"), ("e.jsonl", ""), ("e.json", "{}")])
def test_empty_input_is_an_input_error(tmp_path, fmt, name, content):
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ValueError, match="no traces found|not readable as native"):
        load_source(str(path), fmt)


@pytest.mark.parametrize(
    ("doc", "fmt"),
    [
        ({"run_id": "n0", "steps": []}, "native"),
        (
            [
                {
                    "span_id": "c1",
                    "name": "chain",
                    "attributes": {"openinference.span.kind": "CHAIN"},
                }
            ],
            "openinference",
        ),
    ],
)
def test_right_format_with_nothing_in_it_is_nothing_to_lint(tmp_path, doc, fmt):
    with pytest.raises(ValueError, match="nothing to lint"):
        load_source(_write(tmp_path, doc), fmt)


def test_a_file_with_some_empty_runs_still_lints(tmp_path):
    chain_only = {
        "span_id": "c1",
        "trace_id": "other",
        "name": "chain",
        "attributes": {"openinference.span.kind": "CHAIN"},
    }
    traces = load_source(_write(tmp_path, [*SPANS, chain_only]), "openinference")
    assert sorted(len(t.tool_calls()) for t in traces) == [0, 2]


def test_a_file_holding_only_part_of_a_split_run_is_not_an_error(tmp_path):
    # A rotating exporter can leave just the run's root CHAIN span in the last file.
    tool_spans = _write_jsonl(tmp_path, SPANS, "part-1.jsonl")
    root = {
        "span_id": "root",
        "trace_id": "t1",
        "name": "agent",
        "attributes": {"openinference.span.kind": "CHAIN"},
    }
    tail = _write_jsonl(tmp_path, [root], "part-2.jsonl")
    loaded = load_sources([tool_spans, tail], "openinference")
    assert [_calls(t) for t, _ in loaded] == [["search", "book"]]


def test_a_file_with_nothing_to_lint_fails_the_whole_check(tmp_path):
    good = _write_jsonl(tmp_path, SPANS, "good.jsonl")
    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")
    with pytest.raises(ValueError, match="empty.jsonl: no traces found"):
        load_sources([good, str(empty)], "openinference")
