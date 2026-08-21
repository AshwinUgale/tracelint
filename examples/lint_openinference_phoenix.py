"""Lint OpenInference / OpenTelemetry agent spans — the traces you already collect.

If you instrument your agent with **OpenInference** (Arize Phoenix, OpenLLMetry, Langfuse-via-OTel,
LangSmith export, ...), tracelint runs *on top of* that telemetry: it reads the spans you already
emit and reports structural defects deterministically, with a CI exit code and no model in the
loop. The :func:`from_otel_spans` adapter normalizes an OpenInference span export into tracelint's
canonical schema, so every rule is written once and reaches the whole ecosystem.

This example is fully **offline and keyless**. It builds a small Phoenix-style span export (flat
dotted ``attributes``, the shape Phoenix emits) for a flight-booking agent that makes two real
mistakes, then lints it two ways:

1. schema-free — only the rules that need no ground truth fire (here: the tool error, R2a);
2. with a ``tools.json``-style :class:`ToolRegistry` — R1 now proves the schema violation a
   ``hard_defect`` and the process exits ``2``, exactly as it would in CI.

The same thing at the command line::

    tracelint check spans.json --format openinference --tools tools.json

Run it::

    python examples/lint_openinference_phoenix.py
"""

from __future__ import annotations

import json
from typing import Any

from tracelint import ToolRegistry, lint_otel_trace, render_report


def _tool_span(
    span_id: str,
    start: str,
    name: str,
    args: dict[str, Any],
    output: Any,
    *,
    error: str | None = None,
) -> dict[str, Any]:
    """Build one Phoenix-style OpenInference TOOL span (attributes as a flat dotted dict).

    Phoenix records ``input.value`` / ``output.value`` as JSON strings and marks a failed span
    with OTel ``status_code == "ERROR"`` — the structured signal R2a reads (never guessed).
    """
    span: dict[str, Any] = {
        "span_id": span_id,
        "trace_id": "book-flight-run",
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


def build_openinference_spans() -> list[dict[str, Any]]:
    """A short agent run captured as OpenInference spans, with two planted defects.

    1. ``book_flight`` is called with only ``flight_id`` — its schema also *requires* ``passenger``
       (R1: a schema violation → ``hard_defect``). ``flight_id`` itself was returned by the prior
       ``search_flights`` call, so it is derivable and R3 stays quiet.
    2. ``charge_card`` returns an error (R2a: a ``hard_event`` from the OTel ERROR status).
    """
    return [
        _tool_span(
            "s1",
            "2024-06-01T10:00:01Z",
            "search_flights",
            {"origin": "SFO", "destination": "NRT"},
            {"flight_id": "AA100", "price": 742},
        ),
        _tool_span(
            "s2",
            "2024-06-01T10:00:02Z",
            "book_flight",
            {"flight_id": "AA100"},  # missing the required 'passenger' field
            {"status": "error", "detail": "passenger is required"},
        ),
        _tool_span(
            "s3",
            "2024-06-01T10:00:03Z",
            "charge_card",
            {"amount": 742},
            {"error": "card_declined"},
            error="payment gateway returned 402",
        ),
    ]


def build_registry() -> ToolRegistry:
    """The tool schemas tracelint checks against — your ``tools.json`` (rarely in the trace)."""
    return ToolRegistry.from_dict(
        {
            "tools": {
                "search_flights": {
                    "schema": {
                        "type": "object",
                        "properties": {
                            "origin": {"type": "string"},
                            "destination": {"type": "string"},
                        },
                        "required": ["origin", "destination"],
                    }
                },
                "book_flight": {
                    "schema": {
                        "type": "object",
                        "properties": {
                            "flight_id": {"type": "string"},
                            "passenger": {"type": "string"},
                        },
                        "required": ["flight_id", "passenger"],
                    }
                },
                "charge_card": {
                    "schema": {
                        "type": "object",
                        "properties": {"amount": {"type": "number"}},
                        "required": ["amount"],
                    },
                    "metadata": {"side_effecting": True},
                },
            }
        }
    )


def main() -> int:
    spans = build_openinference_spans()

    print("=== schema-free (no tools.json) — only ground-truth-free rules fire ===")
    print(render_report(lint_otel_trace(spans), include_candidates=True))
    print()

    print("=== with tools.json — R1 proves the schema violation a hard_defect ===")
    report = lint_otel_trace(spans, registry=build_registry())
    print(render_report(report, include_candidates=True))
    return report.exit_code  # 2 — usable directly as a CI gate


if __name__ == "__main__":
    raise SystemExit(main())
