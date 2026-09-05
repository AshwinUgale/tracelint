"""Lint a real **Langflow** trace — captured via OpenInference.

Langflow's Arize/Phoenix tracer instruments its Agent with ``LangChainInstrumentor`` (see
``langflow/services/tracing/arize_phoenix.py``), so a Langflow agent flow emits LangChain-shaped
OpenInference spans. tracelint reads them through the same shared adapter — **no Langflow-specific
code** — which is why enabling the Phoenix (or Langfuse) tracer in Langflow is all a user needs.

Fully **offline and keyless**: ``traces/langflow_trace.json`` was captured from Langflow's shipped
"Simple Agent" starter flow, run on ``gpt-4o-mini`` (the agent fetched a URL via its tool). This
script just lints it — it reproduces with no API key, and the real trace lints **clean** (exit 0).

The same thing at the command line::

    tracelint check examples/traces/langflow_trace.json --format openinference

Run it::

    python examples/lint_langflow.py
"""

from __future__ import annotations

import json
from pathlib import Path

from tracelint import lint_otel_trace, render_report

TRACE = Path(__file__).parent / "traces" / "langflow_trace.json"


def load_spans() -> list[dict]:
    """The captured OpenInference spans from the real Langflow flow run."""
    return json.loads(TRACE.read_text(encoding="utf-8"))


def main() -> int:
    report = lint_otel_trace(load_spans())
    print(render_report(report, include_candidates=True))
    return report.exit_code  # 0 — a real trace, clean, no config


if __name__ == "__main__":
    raise SystemExit(main())
