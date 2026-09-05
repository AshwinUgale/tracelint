"""Lint a real **CrewAI** trace — captured via OpenInference.

A CrewAI crew instrumented with ``openinference-instrumentation-crewai`` emits agent / task / tool
spans. The TOOL span records the arguments as a canonical JSON object, so tracelint reads the call
and result with **no framework-specific code**.

Fully **offline and keyless**: ``traces/crewai_trace.json`` was captured from a real ``gpt-4o-mini``
crew run (a support agent refunding an order). This script just lints it — it reproduces with no API
key.

Note the one **candidate** finding: CrewAI's instrumentation traces the agent / task / tool but not
the LLM turn, so no LLM span carries the user's request. With no observed origin for the argument,
R3 raises a *candidate* (possible-false-positive) — it never fails CI (exit 0), and it clears the
moment the trace also includes an LLM span with the user turn (e.g. via an LLM instrumentor). This
is a coverage characteristic of CrewAI's instrumentation, not a defect in the agent.

The same thing at the command line::

    tracelint check examples/traces/crewai_trace.json --format openinference --include-candidates

Run it::

    python examples/lint_crewai.py
"""

from __future__ import annotations

import json
from pathlib import Path

from tracelint import lint_otel_trace, render_report

TRACE = Path(__file__).parent / "traces" / "crewai_trace.json"


def load_spans() -> list[dict]:
    """The captured OpenInference spans from the real CrewAI run."""
    return json.loads(TRACE.read_text(encoding="utf-8"))


def main() -> int:
    report = lint_otel_trace(load_spans())
    print(render_report(report, include_candidates=True))
    return report.exit_code  # 0 — the lone R3 candidate never fails CI


if __name__ == "__main__":
    raise SystemExit(main())
