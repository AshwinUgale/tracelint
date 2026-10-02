"""Lint your Arize Phoenix agent traces with tracelint — deterministic, no LLM judge.

If you run tool-calling agents and collect their traces in **Arize Phoenix**, tracelint runs *on
top of* that telemetry. It reads the spans you already have and reports **structural** defects —
ignored tool errors, schema-violating calls, hallucinated arguments, loops, duplicate side effects —
each with the exact span as evidence and a CI exit code. No second model judges the trace; for this
class of bug a judge is the wrong tool, because the defect is decidable by *looking at the trace*.

--------------------------------------------------------------------------------------------------
Live Phoenix (the traces you already collect)
--------------------------------------------------------------------------------------------------
Phoenix hands you spans as a dataframe; tracelint reads that exact shape.

    from phoenix.client import Client

    spans = Client().spans.get_spans_dataframe(project_name="my-agent")   # your real agent traces
    spans.to_json("spans.json", orient="records", date_format="iso")

Then lint them — one report per trace — in Python:

    from tracelint import ToolRegistry, load_source, default_rules, lint_trace, render_report

    registry = ToolRegistry.load("tools.json")   # optional: declares side_effecting / failure_when
    for trace in load_source("spans.json", "openinference"):
        print(render_report(lint_trace(trace, default_rules(), registry), include_candidates=True))

…or at the command line, with a CI exit code (2 on a hard defect):

    tracelint check spans.json --format openinference --tools tools.json

--------------------------------------------------------------------------------------------------
This file is a fully offline, keyless demo
--------------------------------------------------------------------------------------------------
It builds an **illustrative** Phoenix-shape span export (constructed, not a captured run) for a
support agent with three planted defects, then lints it — no Phoenix instance or API key required.
Swap in your own `spans.json` export (see above) to lint real traces:

    python examples/lint_phoenix_traces.py
"""

from __future__ import annotations

import json
from typing import Any

from tracelint import ToolRegistry, lint_otel_trace, render_report


def _tool_span(
    span_id: str, start: str, name: str, args: dict[str, Any], output: Any, *, error: str | None
) -> dict[str, Any]:
    """One Phoenix-style OpenInference TOOL span (``attributes`` as a flat dotted dict).

    Phoenix records ``input.value`` / ``output.value`` as JSON strings and marks a failed span with
    the OTel ``status_code == "ERROR"`` — the structured signal R2a reads, never guessed.
    """
    span: dict[str, Any] = {
        "span_id": span_id,
        "trace_id": "support-run",
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
    if error:
        span["status_message"] = error
    return span


def build_phoenix_spans() -> list[dict[str, Any]]:
    """An illustrative support-agent trace, constructed in Phoenix's OpenInference span format
    (not a captured run), with three planted defects.

    The user asks to refund order A100. Then:
      1. ``get_order`` **errors** (500) — but the run continues (R2a: a hard_event from the OTel
         ERROR status).
      2. the failed response still carries a cached payment method (``pm_7731``), and the agent
         **refunds to it**: a side-effecting ``refund_order`` fed a value only the failed call
         returned (R2b: a hard_defect — data from a failed call fed into a real-world action).
      3. ``refund_order`` is called **twice** with the same arguments and the first succeeded
         (R8: a hard_event — a duplicate side effect, i.e. a double refund).

    The opening LLM span carries ``llm.input_messages`` — what the model was asked — so provenance
    (R3) can see the order id came from the user and does not flag it, and R2b can tell that the
    payment method came only from the failed lookup.
    """
    return [
        {
            "span_id": "s0",
            "trace_id": "support-run",
            "start_time": "2024-06-01T10:00:00Z",
            "name": "agent",
            "status_code": "OK",
            "attributes": {
                "openinference.span.kind": "LLM",
                "llm.input_messages.0.message.role": "user",
                "llm.input_messages.0.message.content": "Refund order A100 to the card on file.",
            },
        },
        _tool_span(
            "s1", "2024-06-01T10:00:01Z", "get_order", {"order_id": "A100"},
            {"order_id": "A100", "status": "error", "payment_method": "pm_7731"},
            error="500 internal error",
        ),
        _tool_span(
            "s2", "2024-06-01T10:00:02Z", "refund_order",
            {"order_id": "A100", "payment_method": "pm_7731"}, {"refunded": True}, error=None,
        ),
        _tool_span(
            "s3", "2024-06-01T10:00:03Z", "refund_order",
            {"order_id": "A100", "payment_method": "pm_7731"}, {"refunded": True}, error=None,
        ),
    ]


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
    spans = build_phoenix_spans()
    report = lint_otel_trace(spans, registry=build_registry())
    print(render_report(report, include_candidates=True))
    print(f"\nprocess exit code: {report.exit_code}  (2 = a hard defect; fails CI)")
    return report.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
