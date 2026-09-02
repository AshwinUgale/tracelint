"""Lint your OpenLLMetry / Traceloop agent traces with tracelint — deterministic, no LLM judge.

If you instrument your agents with **OpenLLMetry / Traceloop**, they emit **OpenTelemetry GenAI**
spans. tracelint runs *on top of* that telemetry: it reads the spans you already export and reports
**structural** defects — ignored tool errors, schema-violating calls, hallucinated arguments, loops,
duplicate side effects — each with the exact span as evidence and a CI exit code. No second model
judges the trace; for this class of bug a judge is the wrong tool, because the defect is
decidable by *looking at the trace*.

--------------------------------------------------------------------------------------------------
Live (the spans you already export)
--------------------------------------------------------------------------------------------------
However you export your OpenLLMetry/Traceloop OTel spans (an OTLP JSON dump from your collector or
backend), tracelint's event-list reader understands the GenAI semantic convention
(`gen_ai.operation.name`) directly:

    from tracelint import ToolRegistry, load_source, default_rules, lint_trace, render_report

    registry = ToolRegistry.load("tools.json")   # optional: declares side_effecting / failure_when
    for trace in load_source("spans.json", "otel"):
        print(render_report(lint_trace(trace, default_rules(), registry), include_candidates=True))

…or at the command line, with a CI exit code (2 on a hard defect):

    tracelint check spans.json --format otel --tools tools.json

--------------------------------------------------------------------------------------------------
This file is a fully offline, keyless demo
--------------------------------------------------------------------------------------------------
It builds an *illustrative* OTel GenAI span export (constructed, not a captured run) for a support
agent with three planted defects, then lints it — no collector or API key required. Swap in your own
`spans.json` export to lint real traces:

    python examples/lint_traceloop_traces.py
"""

from __future__ import annotations

import json
from typing import Any

from tracelint import ToolRegistry, lint_otel_trace, render_report

_TRACE_ID = "support-run"


def _tool_span(
    span_id: str, start: str, name: str, args: dict[str, Any], output: Any, *, errored: bool
) -> dict[str, Any]:
    """One OpenLLMetry/Traceloop GenAI ``execute_tool`` span.

    GenAI marks the tool via ``gen_ai.operation.name == "execute_tool"`` and carries plain
    ``input`` / ``output`` (JSON strings). A failure is the structured OTel ``status`` — the signal
    R2a reads, never guessed.
    """
    span: dict[str, Any] = {
        "span_id": span_id,
        "trace_id": _TRACE_ID,
        "start_time": start,
        "name": name,
        "attributes": {
            "gen_ai.operation.name": "execute_tool",
            "gen_ai.tool.name": name,
            "input": json.dumps(args),
            "output": json.dumps(output),
        },
    }
    if errored:
        span["status"] = {"status_code": "ERROR", "description": "500 internal error"}
    else:
        span["status_code"] = "OK"
    return span


def build_genai_spans() -> list[dict[str, Any]]:
    """An illustrative agent trace, constructed in OTel GenAI format (not a captured run), with
    three planted defects: the user asks to refund order A100, ``get_order`` **errors**, and the
    agent refunds anyway using the errored id (R2b hard_defect) — twice (R8). The opening ``chat``
    span carries ``gen_ai.input.messages`` so provenance (R3) sees the id came from the user."""
    return [
        {
            "span_id": "s0",
            "trace_id": _TRACE_ID,
            "start_time": "0",
            "status_code": "OK",
            "attributes": {
                "gen_ai.operation.name": "chat",
                "gen_ai.input.messages": json.dumps(
                    [
                        {
                            "role": "user",
                            "parts": [
                                {"type": "text", "content": "Refund order A100."}
                            ],
                        }
                    ]
                ),
            },
        },
        _tool_span(
            "s1", "1", "get_order", {"order_id": "A100"},
            {"order_id": "A100", "status": "error"}, errored=True,
        ),
        _tool_span(
            "s2", "2", "refund_order", {"order_id": "A100"}, {"refunded": True}, errored=False
        ),
        _tool_span(
            "s3", "3", "refund_order", {"order_id": "A100"}, {"refunded": True}, errored=False
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
    report = lint_otel_trace(build_genai_spans(), registry=build_registry())
    print(render_report(report, include_candidates=True))
    print(f"\nprocess exit code: {report.exit_code}  (2 = a hard defect; fails CI)")
    return report.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
