"""Langfuse integration — tracelint as a citizen of Langfuse.

Fetch a trace the user already collects, run the deterministic checks, and (opt-in) write the
verdict back into Langfuse's own **Score** model so it appears beside their normal evals — a
trace-level pass/defect-count plus each certain finding attached to the *exact offending
observation*, with the evidence in the comment.

Read-only by default; write-back is explicit. Scores are keyed by a stable
:func:`~tracelint.identity.finding_fingerprint` so re-running updates in place rather than
duplicating. (Langfuse dedupes an ingested score on ``id`` + ``name`` + ``timestamp``; a stable id
covers re-runs of the same finding, and the same name is reused, so in practice a re-run replaces
the prior score. If a future SDK requires a pinned timestamp for exact upsert, thread it here.)

The Langfuse SDK is an optional dependency (``pip install "tracelint[langfuse]"``); it is imported
lazily so importing tracelint never requires it. Nothing Langfuse-specific lives in the adapter or
the rule engine.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from tracelint.adapters.langfuse import from_langfuse_trace
from tracelint.findings import LintReport
from tracelint.integrations.base import ScorePlan, plan_scores
from tracelint.rules import default_rules, lint_trace
from tracelint.tools import ToolRegistry
from tracelint.trace import Trace


def _default_client() -> Any:
    """A Langfuse client from the standard ``LANGFUSE_*`` env vars (lazy import)."""
    try:
        from langfuse import get_client
    except ImportError as exc:  # pragma: no cover - exercised only without the extra installed
        raise RuntimeError(
            'the Langfuse integration needs the Langfuse SDK — install it with '
            '`pip install "tracelint[langfuse]"`'
        ) from exc
    return get_client()


@dataclass
class LangfuseCheckResult:
    """The outcome of checking one Langfuse trace."""

    trace: Trace
    report: LintReport
    plans: list[ScorePlan]
    written: int  # scores actually written back (0 unless write_back=True)


class LangfuseIntegration:
    """Fetch a Langfuse trace, lint it, and optionally write findings back as Scores."""

    provider = "langfuse"

    def __init__(self, client: Any | None = None) -> None:
        # An injected client (e.g. in tests) short-circuits the lazy SDK import.
        self._client = client

    def client(self) -> Any:
        if self._client is None:
            self._client = _default_client()
        return self._client

    def fetch_trace(self, trace_id: str, *, tool_names: list[str] | None = None) -> Trace:
        raw = self.client().api.trace.get(trace_id)
        return from_langfuse_trace(raw, tool_names=tool_names)

    def check(
        self,
        trace_id: str,
        *,
        registry: ToolRegistry | None = None,
        tool_names: list[str] | None = None,
        write_back: bool = False,
    ) -> LangfuseCheckResult:
        trace = self.fetch_trace(trace_id, tool_names=tool_names)
        report = lint_trace(trace, default_rules(), registry or ToolRegistry())
        plans = plan_scores(trace, report, scope=trace_id)
        written = self._write(trace_id, plans) if write_back else 0
        return LangfuseCheckResult(trace=trace, report=report, plans=plans, written=written)

    def _write(self, trace_id: str, plans: list[ScorePlan]) -> int:
        client = self.client()
        written = 0
        for plan in plans:
            kwargs: dict[str, Any] = {
                "name": plan.name,
                "value": plan.value,
                "data_type": plan.data_type,
                "trace_id": trace_id,
                "score_id": plan.score_id,
            }
            if plan.observation_id:
                kwargs["observation_id"] = plan.observation_id
            if plan.comment:
                kwargs["comment"] = plan.comment
            client.create_score(**kwargs)
            written += 1
        return written
