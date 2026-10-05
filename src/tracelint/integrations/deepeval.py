"""DeepEval integration — run tracelint as a DeepEval metric.

DeepEval (https://deepeval.com) is an LLM-eval framework: you assemble ``LLMTestCase`` objects and
score them with metrics. This exposes tracelint's deterministic trace checks as one such metric, so
a team already running DeepEval gets a structural-reliability check in the same suite, with the same
pass/fail gate as ``tracelint check`` — no judge, no tokens, just the rules.

tracelint scores a *trace*, not an LLM's text output, so the trace reaches the metric one of two
ways:

- **bound at construction** — ``TracelintMetric(trace=...)``, when one metric checks one trace; or
- **per test case** — ``LLMTestCase(..., additional_metadata={"tracelint_trace": <trace>})``, the
  idiomatic DeepEval flow where ``evaluate([cases], [metric])`` runs the metric over many cases.

A trace is a :class:`~tracelint.trace.Trace`, or a path to a trace file read with ``fmt`` (the same
formats ``tracelint check --format`` accepts). The score is **1.0 when the trace passes the gate and
0.0 otherwise** — deterministic, so a verdict, not a probability — and ``success`` is exactly
``tracelint check``'s exit-0 condition under the chosen ``fail_on`` tier.

The DeepEval SDK is an optional dependency (``pip install "tracelint[deepeval]"``); it is imported
lazily, so importing this module — and its pure core, :func:`score_trace` — never requires it. The
``TracelintMetric`` symbol is built on first access (PEP 562), when DeepEval must be importable.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tracelint.findings import ConfidenceTier, LintReport
from tracelint.rules import Rule, lint_trace, select_rules
from tracelint.sources import load_source
from tracelint.tools import ToolRegistry
from tracelint.trace import Trace

#: Default key under an ``LLMTestCase.additional_metadata`` that carries the trace to check.
DEFAULT_TRACE_KEY = "tracelint_trace"


@dataclass
class TracelintScore:
    """The outcome of scoring one trace: a DeepEval-shaped verdict plus the full report."""

    score: float  # 1.0 passed the gate, 0.0 failed it
    success: bool
    reason: str
    report: LintReport


def _reason(report: LintReport) -> str:
    """A compact, DeepEval-facing summary: the verdict line, then each active finding."""
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
    """Lint ``trace`` and turn the report into a pass/fail score (no DeepEval needed).

    ``success`` is exactly ``tracelint check``'s clean exit under ``fail_on``: a hard defect always
    fails; ``fail_on`` opts the gate down to ``hard_event`` or ``candidate``. ``score`` is 1.0 on a
    pass and 0.0 on a fail.
    """
    chosen = rules if rules is not None else select_rules(None)
    report = lint_trace(trace, chosen, registry or ToolRegistry())
    report.fail_on = fail_on
    passed = report.exit_code == 0
    return TracelintScore(1.0 if passed else 0.0, passed, _reason(report), report)


def resolve_registry(tools: str | Path | ToolRegistry | None) -> ToolRegistry:
    """A :class:`ToolRegistry` from a registry (as-is), a path to ``tools.json``, or nothing."""
    if isinstance(tools, ToolRegistry):
        return tools
    if tools is None:
        return ToolRegistry()
    return ToolRegistry.load(str(tools))


def resolve_trace(value: Trace | str | Path, *, fmt: str = "native") -> Trace:
    """A :class:`Trace` from a Trace (as-is) or a path to a trace file read with ``fmt``.

    A file holding several traces (e.g. a span export) is rejected: a metric scores one run, so the
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


def _as_tier(fail_on: str | ConfidenceTier) -> ConfidenceTier:
    return fail_on if isinstance(fail_on, ConfidenceTier) else ConfidenceTier(fail_on)


def _trace_from_test_case(test_case: Any, trace_key: str) -> Any:
    meta = getattr(test_case, "additional_metadata", None) or {}
    try:
        return meta.get(trace_key)
    except AttributeError:  # additional_metadata isn't a mapping
        return None


def _import_base_metric() -> type:
    try:
        from deepeval.metrics import BaseMetric
    except ImportError as exc:  # pragma: no cover - only without the extra installed
        raise RuntimeError(
            "the DeepEval integration needs the DeepEval SDK — install it with "
            '`pip install "tracelint[deepeval]"`'
        ) from exc
    return BaseMetric


def _build_metric_class() -> type:
    """Define ``TracelintMetric`` against DeepEval's ``BaseMetric`` (imported lazily)."""
    base = _import_base_metric()

    class TracelintMetric(base):  # type: ignore[valid-type,misc]
        """A DeepEval metric that passes a test case iff its trace passes tracelint's checks.

        The trace comes from ``trace=`` (bound) or, per test case, from
        ``test_case.additional_metadata[trace_key]``. ``tools`` / ``rules`` / ``fmt`` / ``fail_on``
        mirror ``tracelint check``. ``score`` is 1.0 on a pass, 0.0 on a fail; ``threshold`` (0.5)
        turns that into ``success``.
        """

        def __init__(
            self,
            *,
            tools: str | Path | ToolRegistry | None = None,
            rules: list[str] | None = None,
            fmt: str = "native",
            fail_on: str | ConfidenceTier = ConfidenceTier.HARD_DEFECT,
            threshold: float = 0.5,
            trace: Trace | str | Path | None = None,
            trace_key: str = DEFAULT_TRACE_KEY,
        ) -> None:
            self.threshold = threshold
            self._registry = resolve_registry(tools)
            self._rules = select_rules(rules)
            self._fmt = fmt
            self._fail_on = _as_tier(fail_on)
            self._trace = trace
            self._trace_key = trace_key
            self.score: float | None = None
            self.success: bool = False
            self.reason: str | None = None
            self.error: str | None = None

        def measure(self, test_case: Any) -> float:
            self.error = None
            source = (
                self._trace
                if self._trace is not None
                else _trace_from_test_case(test_case, self._trace_key)
            )
            if source is None:
                self.score, self.success = 0.0, False
                self.reason = (
                    "no trace to check: pass trace=... to the metric, or set "
                    f"additional_metadata[{self._trace_key!r}] on the test case."
                )
                self.error = self.reason
                return self.score
            try:
                trace = resolve_trace(source, fmt=self._fmt)
                result = score_trace(
                    trace, registry=self._registry, rules=self._rules, fail_on=self._fail_on
                )
            except Exception as exc:  # a bad path / unreadable trace shouldn't crash the suite
                self.score, self.success = 0.0, False
                self.reason = f"tracelint could not check the trace: {exc}"
                self.error = str(exc)
                return self.score
            self.score, self.success, self.reason = result.score, result.success, result.reason
            return self.score

        async def a_measure(self, test_case: Any) -> float:
            return self.measure(test_case)

        def is_successful(self) -> bool:
            return self.success

        @property
        def __name__(self) -> str:
            return "tracelint"

    return TracelintMetric


_METRIC_CLASS: type | None = None


def __getattr__(name: str) -> Any:
    # PEP 562: build TracelintMetric on first access so the module imports without DeepEval.
    if name == "TracelintMetric":
        global _METRIC_CLASS
        if _METRIC_CLASS is None:
            _METRIC_CLASS = _build_metric_class()
        return _METRIC_CLASS
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
