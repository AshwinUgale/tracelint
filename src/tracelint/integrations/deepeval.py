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

from pathlib import Path
from typing import Any

from tracelint.findings import ConfidenceTier
from tracelint.integrations.scoring import (
    DEFAULT_TRACE_KEY,
    TracelintScore,
    as_tier,
    resolve_registry,
    resolve_trace,
    score_trace,
)
from tracelint.rules import select_rules
from tracelint.tools import ToolRegistry
from tracelint.trace import Trace

# The scoring core is shared with the other eval-harness wrappers (see scoring.py); re-exported here
# so ``from tracelint.integrations.deepeval import score_trace`` keeps working.
__all__ = [
    "DEFAULT_TRACE_KEY",
    "TracelintScore",
    "resolve_registry",
    "resolve_trace",
    "score_trace",
]


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
            self._fail_on = as_tier(fail_on)
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
