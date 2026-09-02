"""Lint your Langfuse agent traces with tracelint — deterministic, no LLM judge.

If you run tool-calling agents and collect their traces in **Langfuse**, tracelint runs *on top of*
that telemetry. It reads the observations you already have and reports **structural** defects —
ignored tool errors, schema-violating calls, hallucinated arguments, loops, duplicate side effects —
each with the exact observation as evidence and a CI exit code. No second model judges the trace;
for this class of bug a judge is the wrong tool, because the defect is decidable by *looking at the
trace*.

--------------------------------------------------------------------------------------------------
Live Langfuse (the traces you already collect)
--------------------------------------------------------------------------------------------------
Fetch a trace with the Langfuse SDK and hand it straight to tracelint — the adapter accepts the SDK
object (or the raw API JSON):

    from langfuse import Langfuse
    from tracelint import ToolRegistry, lint_langfuse_trace, render_report

    langfuse = Langfuse()                           # reads your LANGFUSE_* env vars
    trace = langfuse.api.trace.get("your-trace-id")  # method varies by SDK version

    registry = ToolRegistry.load("tools.json")       # optional: side_effecting / failure_when
    print(render_report(lint_langfuse_trace(trace, registry=registry), include_candidates=True))

Tool observations that don't use the v4 `tool` type are still recognized if you pass
`tool_names={...}` (usually the same tool names as your `tools.json`). At the command line, with a
CI exit code (2 on a hard defect):

    tracelint check trace.json --format langfuse --tools tools.json

--------------------------------------------------------------------------------------------------
This file is a fully offline, keyless demo
--------------------------------------------------------------------------------------------------
It builds an *illustrative* Langfuse trace (constructed, not a captured run) for a support agent
with three planted defects, then lints it — no Langfuse project or API key required. Swap in your
own fetched trace (see above) to lint real runs:

    python examples/lint_langfuse_traces.py
"""

from __future__ import annotations

from typing import Any

from tracelint import ToolRegistry, lint_langfuse_trace, render_report


def build_langfuse_trace() -> dict[str, Any]:
    """An illustrative Langfuse trace (constructed, not a captured run) with three planted defects.

    The user asks to refund order A100. Then:
      1. `get_order` **errors** (Langfuse `level == "ERROR"`) — but the run continues (R2a: a
         hard_event read straight from the structured error level).
      2. the errored order id is **reused** as the argument to a side-effecting `refund_order`
         (R2b: a hard_defect — data from a failed call fed into a real-world action, no fallback).
      3. `refund_order` is called **twice** with the same arguments and the first succeeded
         (R8: a hard_event — a duplicate side effect, i.e. a double refund).

    The trace-level `input` (the user's request) seeds provenance, so R3 sees the order id came
    from the user and does not flag it.
    """
    return {
        "id": "support-run",
        "input": "Refund order A100 to the card on file.",
        "output": "Your refund is processed.",
        "observations": [
            {
                "id": "o1", "type": "tool", "name": "get_order",
                "input": {"order_id": "A100"},
                "output": {"order_id": "A100", "status": "error"},
                "level": "ERROR", "statusMessage": "500 internal error",
                "startTime": "2024-06-01T10:00:01Z",
            },
            {
                "id": "o2", "type": "tool", "name": "refund_order",
                "input": {"order_id": "A100"}, "output": {"refunded": True},
                "startTime": "2024-06-01T10:00:02Z",
            },
            {
                "id": "o3", "type": "tool", "name": "refund_order",
                "input": {"order_id": "A100"}, "output": {"refunded": True},
                "startTime": "2024-06-01T10:00:03Z",
            },
        ],
    }


def build_registry() -> ToolRegistry:
    """The operator's `tools.json`: `refund_order` mutates the world, and a `{refunded: false}`
    body is its declared failure. (Declared once, never guessed from the tool's name.)"""
    return ToolRegistry.from_dict(
        {
            "tools": {
                "get_order": {},
                "refund_order": {
                    "metadata": {
                        "side_effecting": True,
                        "failure_when": {"pointer": "/refunded", "equals": False},
                    }
                },
            }
        }
    )


def main() -> int:
    report = lint_langfuse_trace(build_langfuse_trace(), registry=build_registry())
    print(render_report(report, include_candidates=True))
    print(f"\nprocess exit code: {report.exit_code}  (2 = a hard defect; fails CI)")
    return report.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
