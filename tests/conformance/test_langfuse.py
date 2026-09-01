"""Conformance: the Langfuse trace reader (tool observations, error level)."""

from __future__ import annotations

from conformance._harness import check
from tracelint.adapters.langfuse import from_langfuse_trace


def test_tool_observations_normal_and_error_level():
    raw = {
        "id": "t1",
        "input": "refund order A100",
        "output": "done",
        "observations": [
            {
                "id": "o1",
                "type": "tool",
                "name": "lookup_order",
                "input": {"order_id": "A100"},
                "output": {"amount": 49.99},
                "startTime": "2024-01-01T00:00:01Z",
            },
            {
                "id": "o2",
                "type": "tool",
                "name": "refund",
                "input": {"order_id": "A100"},
                "output": {"error": "already refunded"},
                "level": "ERROR",
                "statusMessage": "dup",
                "startTime": "2024-01-01T00:00:02Z",
            },
        ],
    }
    check(
        from_langfuse_trace(raw),
        [
            ("message", "user", "refund order A100"),
            ("tool_call", "lookup_order", {"order_id": "A100"}),
            ("tool_result", "lookup_order", "unknown", None, None, {"amount": 49.99}),
            ("tool_call", "refund", {"order_id": "A100"}),
            (
                "tool_result", "refund", "error", None,
                "already refunded", {"error": "already refunded"},
            ),
        ],
    )
