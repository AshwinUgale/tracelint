"""Conformance: the LangSmith run-tree reader (nested runs, temporal order, tool error)."""

from __future__ import annotations

from conformance._harness import check
from tracelint.adapters.langsmith import from_langsmith_run


def test_nested_run_tree_orders_by_start_time_and_preserves_error():
    raw = {
        "id": "root",
        "run_type": "chain",
        "inputs": {"input": "Cancel order Z999."},
        "outputs": {"output": "done"},
        "child_runs": [
            {
                "id": "lookup",
                "run_type": "tool",
                "name": "lookup_order",
                "inputs": {"order_id": "Z999"},
                "outputs": {"status": "missing"},
                "error": "order not found",
                "start_time": "2024-01-01T00:00:01Z",
            },
            {
                "id": "cancel",
                "run_type": "tool",
                "name": "cancel_order",
                "inputs": {"order_id": "Z999"},
                "outputs": {"status": "ok"},
                "start_time": "2024-01-01T00:00:02Z",
            },
        ],
    }
    check(
        from_langsmith_run(raw),
        [
            ("message", "user", "Cancel order Z999."),
            ("tool_call", "lookup_order", {"order_id": "Z999"}),
            (
                "tool_result", "lookup_order", "error", None,
                "order not found", {"status": "missing"},
            ),
            ("tool_call", "cancel_order", {"order_id": "Z999"}),
            # LangSmith's explicit status field maps "ok" -> OK (a bare OTel span stays UNKNOWN).
            ("tool_result", "cancel_order", "ok", None, None, {"status": "ok"}),
        ],
    )
