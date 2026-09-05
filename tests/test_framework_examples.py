"""Real captured framework traces lint as documented — CI-enforced regression fixtures.

Each trace in ``examples/traces/`` was captured from a real gpt-4o-mini run of the named framework,
instrumented with that framework's stock OpenInference package. These assertions pin the headline
adoption claim ("real <framework> trace → tracelint via --format openinference, zero config") and
guard the shared OTel adapter against regressions that would break a whole framework at once.
"""

from __future__ import annotations

from examples.lint_crewai import load_spans as crewai_spans
from examples.lint_langflow import load_spans as langflow_spans
from examples.lint_langgraph import load_spans as langgraph_spans
from examples.lint_smolagents import load_spans as smolagents_spans
from tracelint import lint_otel_trace
from tracelint.findings import ConfidenceTier


def test_smolagents_real_trace_lints_clean():
    report = lint_otel_trace(smolagents_spans())
    assert not report.has_hard_defect
    assert report.exit_code == 0
    assert report.active_findings == []


def test_langgraph_real_trace_lints_clean():
    # Exercises the LangChain arg-recovery path (TOOL input is lossy; real args on the LLM span).
    report = lint_otel_trace(langgraph_spans())
    assert not report.has_hard_defect
    assert report.exit_code == 0
    assert report.active_findings == []


def test_langflow_real_trace_lints_clean():
    # Langflow's Phoenix tracer is LangChainInstrumentor, so this is LangChain-shaped too.
    report = lint_otel_trace(langflow_spans())
    assert not report.has_hard_defect
    assert report.exit_code == 0
    assert report.active_findings == []


def test_crewai_real_trace_has_only_the_provenance_candidate():
    # CrewAI instruments agent/task/tool but not the LLM turn, so there is no user-turn provenance
    # for R3 — a *candidate* (never fails CI), not a hard defect. The tool/arg path reads cleanly.
    report = lint_otel_trace(crewai_spans())
    assert not report.has_hard_defect
    assert report.exit_code == 0
    candidates = report.by_tier(ConfidenceTier.CANDIDATE)
    assert [f.rule for f in candidates] == ["R3"]
