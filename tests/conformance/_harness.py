"""Adapter conformance harness.

A conformance fixture pins *normalization* only: raw provider payload -> exact canonical steps.
No rules run — the single question is "did the adapter correctly understand what happened?" These
fixtures are the regression guard that matters most, because in practice a linter that confidently
*misreads* a real trace is worse than one missing a rule.

``normalized`` renders a canonical :class:`Trace` as a compact, comparable list: step order, message
roles + text, tool names + args, and each result's paired-call name / status / http / error /
content. Adapter-synthesized ``call_id`` values are deliberately *not* compared — result→call
pairing is checked structurally by resolving each result back to its call's name.
"""

from __future__ import annotations

from tracelint.trace import Message, ToolCall, ToolResult, Trace


def normalized(trace: Trace) -> list[tuple]:
    out: list[tuple] = []
    for step in trace.steps:
        if isinstance(step, Message):
            out.append(("message", step.role.value, step.content))
        elif isinstance(step, ToolCall):
            out.append(("tool_call", step.name, step.args))
        elif isinstance(step, ToolResult):
            call = trace.call_for(step)
            out.append(
                (
                    "tool_result",
                    call.name if call else None,
                    step.status.value,
                    step.http_status,
                    step.error,
                    step.content,
                )
            )
    return out


def check(trace: Trace, expected: list[tuple]) -> None:
    """Assert a normalized trace equals ``expected``, with a readable diff on failure."""
    actual = normalized(trace)
    assert actual == expected, (
        f"conformance mismatch:\n  expected: {expected}\n  actual:   {actual}"
    )
