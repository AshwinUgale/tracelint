"""Shared, dependency-free scoring core for the eval-harness integrations.

DeepEval and promptfoo both answer the same question — *does this run's trace pass tracelint's
checks?* — and differ only in how the harness hands over the trace and reports the verdict. This
module is that common answer: lint a trace and turn the report into a pass/fail score with the same
gate as ``tracelint check``, with no harness SDK involved. Each wrapper (``deepeval.py``,
``promptfoo.py``) is a thin shell over it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from tracelint.findings import ConfidenceTier, LintReport
from tracelint.rules import Rule, lint_trace, select_rules
from tracelint.sources import load_source
from tracelint.tools import ToolRegistry
from tracelint.trace import Trace

#: Default key (a DeepEval test case's ``additional_metadata``, or a promptfoo ``vars`` entry) that
#: carries the trace to check.
DEFAULT_TRACE_KEY = "tracelint_trace"


@dataclass
class TracelintScore:
    """The outcome of scoring one trace: a harness-shaped verdict plus the full report."""

    score: float  # 1.0 passed the gate, 0.0 failed it
    success: bool
    reason: str
    report: LintReport


def reason_for(report: LintReport) -> str:
    """A compact, harness-facing summary: the verdict line, then each active finding."""
    verdict = "passed" if report.exit_code == 0 else "failed"
    lines = [
        f"tracelint {verdict}: {len(report.active_findings)} finding(s), exit {report.exit_code}"
    ]
    for f in report.active_findings:
        loc = "step " + ",".join(str(i) for i in f.step_indices) if f.step_indices else "no step"
        lines.append(f"  [{f.tier.value}] {f.rule} {f.finding_type} ({loc}): {f.summary}")
    if report.exit_code == 0 and not report.active_findings:
        lines.append("  clean — no structural issues found.")
    return "\n".join(lines)


def score_trace(
    trace: Trace,
    *,
    registry: ToolRegistry | None = None,
    rules: list[Rule] | None = None,
    fail_on: ConfidenceTier = ConfidenceTier.HARD_DEFECT,
) -> TracelintScore:
    """Lint ``trace`` and turn the report into a pass/fail score (no harness SDK needed).

    ``success`` is exactly ``tracelint check``'s clean exit under ``fail_on``: a hard defect always
    fails; ``fail_on`` opts the gate down to ``hard_event`` or ``candidate``. ``score`` is 1.0 on a
    pass and 0.0 on a fail.
    """
    chosen = rules if rules is not None else select_rules(None)
    report = lint_trace(trace, chosen, registry if registry is not None else ToolRegistry())
    report.fail_on = fail_on
    passed = report.exit_code == 0
    return TracelintScore(1.0 if passed else 0.0, passed, reason_for(report), report)


def resolve_registry(tools: str | Path | ToolRegistry | None) -> ToolRegistry:
    """A :class:`ToolRegistry` from a registry (as-is), a path to ``tools.json``, or nothing."""
    if isinstance(tools, ToolRegistry):
        return tools
    if tools is None:
        return ToolRegistry()
    return ToolRegistry.load(str(tools))


def resolve_trace(value: Trace | str | Path, *, fmt: str = "native") -> Trace:
    """A :class:`Trace` from a Trace (as-is) or a path to a trace file read with ``fmt``.

    A file holding several traces (e.g. a span export) is rejected: a score is about one run, so the
    caller must pass the specific trace. Raises ``ValueError`` on anything else.
    """
    if isinstance(value, Trace):
        return value
    if isinstance(value, (str, Path)):
        traces = load_source(value, fmt)
        if len(traces) != 1:
            raise ValueError(
                f"{value!r} holds {len(traces)} traces; pass a single Trace, or a file with one run"
            )
        return traces[0]
    raise ValueError(f"cannot read a trace from {type(value).__name__}; pass a Trace or a path")


def as_tier(fail_on: str | ConfidenceTier) -> ConfidenceTier:
    """``fail_on`` as a :class:`ConfidenceTier`, accepting the tier itself or its string name."""
    return fail_on if isinstance(fail_on, ConfidenceTier) else ConfidenceTier(fail_on)
