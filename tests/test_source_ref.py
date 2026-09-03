"""SourceRef — provider identity carried on canonical steps.

Adapters record where a step came from (provider + trace/span/observation id) so an integration
can later attach a finding to the exact offending record. It's optional, serializes round-trip,
and the rule engine never reads it.
"""

from __future__ import annotations

from tracelint.adapters.langfuse import from_langfuse_trace
from tracelint.trace import SourceRef, ToolCall, Trace


def test_sourceref_to_dict_drops_none():
    assert SourceRef(provider="langfuse", observation_id="o1").to_dict() == {
        "provider": "langfuse",
        "observation_id": "o1",
    }


def test_sourceref_from_dict_roundtrip_and_none():
    src = SourceRef(provider="langfuse", trace_id="t1", observation_id="o1")
    assert SourceRef.from_dict(src.to_dict()) == src
    assert SourceRef.from_dict(None) is None
    assert SourceRef.from_dict({}) is None


def test_step_source_survives_trace_serialization():
    trace = Trace(
        run_id="r",
        steps=[
            ToolCall(
                call_id="c1",
                name="refund",
                args={"order_id": "A100"},
                source=SourceRef(provider="langfuse", trace_id="t1", observation_id="o1"),
            )
        ],
    )
    restored = Trace.from_dict(trace.to_dict())
    src = restored.tool_calls()[0].source
    assert src == SourceRef(provider="langfuse", trace_id="t1", observation_id="o1")


def test_step_source_absent_by_default():
    trace = Trace(run_id="r", steps=[ToolCall(call_id="c1", name="x")])
    assert trace.tool_calls()[0].source is None
    assert "source" not in trace.to_dict()["steps"][0]


def test_langfuse_adapter_populates_source_on_tool_steps():
    raw = {
        "id": "t1",
        "input": "refund order A100",
        "observations": [
            {
                "id": "o1",
                "type": "tool",
                "name": "refund",
                "input": {"order_id": "A100"},
                "output": {"refunded": True},
                "startTime": "2024-01-01T00:00:01Z",
            }
        ],
    }
    trace = from_langfuse_trace(raw)
    call_src = trace.tool_calls()[0].source
    result_src = trace.tool_results()[0].source
    assert call_src == SourceRef(provider="langfuse", trace_id="t1", observation_id="o1")
    assert result_src == SourceRef(provider="langfuse", trace_id="t1", observation_id="o1")
