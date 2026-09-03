"""Provider-neutral score planning for write-back.

Turning a :class:`~tracelint.findings.LintReport` into the small, bounded set of scores an
observability platform should show — deliberately restrained, so a trace's score panel stays
readable instead of drowning in one score per rule:

- **trace level**: ``tracelint.passed`` (BOOLEAN) and ``tracelint.hard_defects`` (NUMERIC) — the
  headline "did this run pass deterministic checks, and how many provable defects".
- **observation level**: one BOOLEAN score per *certain* finding (``hard_defect`` / ``hard_event``),
  attached to the exact offending observation when the trace carries its source id, with the
  evidence in the comment. Heuristic ``candidate`` findings are **not** written back in v1 — they
  are shown in the terminal report for review, not asserted into the user's platform.

Each plan carries a stable ``score_id`` (from :func:`~tracelint.identity.finding_fingerprint`) so a
re-run **updates** the existing score rather than creating a duplicate. This module knows nothing
about any vendor SDK; an integration translates a :class:`ScorePlan` into a provider call.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from tracelint.findings import ConfidenceTier, Finding, LintReport
from tracelint.identity import finding_fingerprint
from tracelint.trace import Trace

# Tiers asserted back into the platform. Candidates are review-only (never written in v1).
_WRITEBACK_TIERS = (ConfidenceTier.HARD_DEFECT, ConfidenceTier.HARD_EVENT)


@dataclass
class ScorePlan:
    """One score to write back, in provider-neutral terms.

    ``observation_id`` is ``None`` for a trace-level score. ``value`` is ``1``/``0`` for a BOOLEAN
    score and a count for a NUMERIC one.
    """

    name: str
    value: float
    data_type: str  # "NUMERIC" | "BOOLEAN" | "CATEGORICAL" | "TEXT"
    score_id: str
    comment: str | None = None
    observation_id: str | None = None


def _scope_hash(scope: str) -> str:
    return hashlib.sha256(scope.encode("utf-8")).hexdigest()[:12]


def _finding_observations(trace: Trace, finding: Finding) -> list[str]:
    """Source observation ids for a finding's evidence steps, in step order."""
    ids: list[str] = []
    for i in finding.step_indices:
        if 0 <= i < len(trace.steps):
            src = trace.steps[i].source
            if src is not None and src.observation_id:
                ids.append(src.observation_id)
    return ids


def plan_scores(trace: Trace, report: LintReport, *, scope: str) -> list[ScorePlan]:
    """The bounded set of scores to write for one linted trace. ``scope`` (the platform trace id)
    keys the stable score ids so re-runs update in place."""
    hard = report.by_tier(ConfidenceTier.HARD_DEFECT)
    plans: list[ScorePlan] = [
        ScorePlan(
            name="tracelint.passed",
            value=0 if report.has_hard_defect else 1,
            data_type="BOOLEAN",
            score_id=f"tracelint-{_scope_hash(scope)}-passed",
        ),
        ScorePlan(
            name="tracelint.hard_defects",
            value=float(len(hard)),
            data_type="NUMERIC",
            score_id=f"tracelint-{_scope_hash(scope)}-hard_defects",
        ),
    ]

    for finding in report.active_findings:
        if finding.tier not in _WRITEBACK_TIERS:
            continue
        obs_ids = _finding_observations(trace, finding)
        step_keys = obs_ids or [str(i) for i in finding.step_indices]
        plans.append(
            ScorePlan(
                name=f"tracelint.{finding.finding_type}",
                value=1,
                data_type="BOOLEAN",
                score_id=finding_fingerprint(finding, scope=scope, step_keys=step_keys),
                comment=finding.summary or None,
                observation_id=obs_ids[-1] if obs_ids else None,
            )
        )
    return plans
