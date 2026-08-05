"""Phase 0 — the canonical trace schema is the contract (spec §II.4)."""

from __future__ import annotations

import json

import pytest

from tracelint import (
    Message,
    ResultStatus,
    Role,
    StepMeta,
    ToolCall,
    ToolResult,
    Trace,
    build_trace,
    load_traces,
)
from tracelint.trace import _step_from_dict


def _sample_trace() -> Trace:
    return build_trace(
        "run-1",
        [
            Message(Role.USER, "cancel order 4521 if it hasn't shipped"),
            ToolCall("c1", "get_order_status", {"order_id": "4521"}),
            ToolResult("c1", {"status": "processing"}, status=ResultStatus.OK),
            ToolCall("c2", "cancel_order", {"order_id": "4521"}),
            ToolResult("c2", {"cancelled": True}, status=ResultStatus.OK),
            Message(Role.ASSISTANT, "Your order has been cancelled."),
        ],
        final="Your order has been cancelled.",
    )


def test_steps_are_indexed_sequentially():
    trace = _sample_trace()
    assert [s.index for s in trace.steps] == [0, 1, 2, 3, 4, 5]
    assert len(trace) == 6


def test_filters_select_by_type():
    trace = _sample_trace()
    assert len(trace.messages()) == 2
    assert [c.name for c in trace.tool_calls()] == ["get_order_status", "cancel_order"]
    assert len(trace.tool_results()) == 2


def test_call_result_pairing_by_call_id():
    trace = _sample_trace()
    call = trace.tool_calls()[0]
    result = trace.result_for(call)
    assert result is not None and result.call_id == "c1"
    assert trace.call_for(result) is call
    pairs = trace.pairs()
    assert len(pairs) == 2
    assert all(res is not None for _, res in pairs)


def test_unmatched_call_is_surfaced_not_hidden():
    # A call whose result was never captured (run ended, or lossy instrumentation).
    trace = build_trace("run-2", [ToolCall("x1", "search", {"q": "refunds"})])
    (call, result) = trace.pairs()[0]
    assert result is None  # observable, not silently invented


def test_result_status_parse_falls_back_to_unknown():
    assert ResultStatus.parse("error") is ResultStatus.ERROR
    assert ResultStatus.parse(None) is ResultStatus.UNKNOWN
    assert ResultStatus.parse("weird") is ResultStatus.UNKNOWN


def test_json_round_trip_preserves_structure():
    trace = _sample_trace()
    restored = Trace.from_json(trace.to_json())
    assert restored.run_id == trace.run_id
    assert restored.final == trace.final
    assert [type(s).__name__ for s in restored.steps] == [type(s).__name__ for s in trace.steps]
    call = restored.tool_calls()[1]
    assert call.name == "cancel_order" and call.args == {"order_id": "4521"}


def test_step_meta_round_trip_and_prunes_empty():
    meta = StepMeta(model="gpt-4o", tokens_in=340, injected=True, fault_injection_id="f7")
    d = meta.to_dict()
    assert d == {
        "model": "gpt-4o",
        "tokens_in": 340,
        "injected": True,
        "fault_injection_id": "f7",
    }
    assert StepMeta.from_dict(d).model == "gpt-4o"
    assert StepMeta.from_dict(None) is None


def test_tool_result_error_signals_survive_serialization():
    res = ToolResult("c9", "boom", status=ResultStatus.ERROR, error="HTTP 500", http_status=500)
    back = _step_from_dict(res.to_dict())
    assert isinstance(back, ToolResult)
    assert back.status is ResultStatus.ERROR and back.http_status == 500


def test_unknown_step_type_raises():
    with pytest.raises(ValueError):
        _step_from_dict({"type": "nonsense"})


def test_load_traces_json_and_jsonl(tmp_path):
    trace = _sample_trace()
    single = tmp_path / "t.json"
    single.write_text(trace.to_json(), encoding="utf-8")
    assert len(load_traces(single)) == 1

    many = tmp_path / "t.jsonl"
    many.write_text(
        trace.to_json(indent=None) + "\n" + trace.to_json(indent=None) + "\n", encoding="utf-8"
    )
    loaded = load_traces(many)
    assert len(loaded) == 2 and loaded[0].run_id == "run-1"

    arr = tmp_path / "arr.json"
    arr.write_text(json.dumps([trace.to_dict(), trace.to_dict()]), encoding="utf-8")
    assert len(load_traces(arr)) == 2
