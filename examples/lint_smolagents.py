"""Lint a real Hugging Face **smolagents** trace — captured via OpenInference.

smolagents' own telemetry tutorial instruments a ``ToolCallingAgent`` with
``openinference-instrumentation-smolagents`` and ships the spans to Phoenix / Langfuse. tracelint
runs *on top of* that same telemetry: it reads the OpenInference spans the agent already emits and
reports structural defects deterministically, with **no framework-specific code**.

Fully **offline and keyless**: ``traces/smolagents_trace.json`` was captured from a real
``gpt-4o-mini`` ``ToolCallingAgent`` run (a support agent asked to refund an order). This script
just lints it, so the result reproduces with no API key. The real trace lints **clean** — the call,
the tool result, and the user turn are all read correctly, and no false positives are raised.

The same thing at the command line::

    tracelint check examples/traces/smolagents_trace.json --format openinference

Run it::

    python examples/lint_smolagents.py
"""

from __future__ import annotations

import json
from pathlib import Path

from tracelint import lint_otel_trace, render_report

TRACE = Path(__file__).parent / "traces" / "smolagents_trace.json"


def load_spans() -> list[dict]:
    """The captured OpenInference spans from the real smolagents run."""
    return json.loads(TRACE.read_text(encoding="utf-8"))


def main() -> int:
    report = lint_otel_trace(load_spans())
    print(render_report(report, include_candidates=True))
    return report.exit_code  # 0 — a real trace, clean, no config


if __name__ == "__main__":
    raise SystemExit(main())
