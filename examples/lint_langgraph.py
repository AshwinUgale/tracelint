"""Lint a real **LangGraph / LangChain** trace — captured via OpenInference.

A LangGraph ``create_react_agent`` instrumented with ``openinference-instrumentation-langchain``
emits OpenInference spans whose shape differs from smolagents': the tool call's structured arguments
live on the **LLM span's** ``tool_calls``, while the TOOL span records only a bare scalar input.
tracelint's shared OpenInference adapter recovers the real arguments from the LLM tool_call, so the
trace lints correctly with **no framework-specific code**.

Fully **offline and keyless**: ``traces/langgraph_trace.json`` was captured from a real
``gpt-4o-mini`` ``create_react_agent`` run. This script just lints it — it reproduces with no API
key, and the real trace lints **clean** (0 findings, exit 0).

The same thing at the command line::

    tracelint check examples/traces/langgraph_trace.json --format openinference

Run it::

    python examples/lint_langgraph.py
"""

from __future__ import annotations

import json
from pathlib import Path

from tracelint import lint_otel_trace, render_report

TRACE = Path(__file__).parent / "traces" / "langgraph_trace.json"


def load_spans() -> list[dict]:
    """The captured OpenInference spans from the real LangGraph run."""
    return json.loads(TRACE.read_text(encoding="utf-8"))


def main() -> int:
    report = lint_otel_trace(load_spans())
    print(render_report(report, include_candidates=True))
    return report.exit_code  # 0 — a real trace, clean, no config


if __name__ == "__main__":
    raise SystemExit(main())
