"""Nondeterminism: one trace is one sample (spec §II.8; learning-doc 03 §3).

An agent at temperature > 0 takes a different path run to run, so a finding seen in one trace is a
*sample*, not the truth. The honest treatment (§II.8): run each scenario ``k`` times, report each
finding's **reproduction rate** with a confidence interval, and flag findings that appear in some
but not all runs as **flaky**. A ``k=1`` run of a nondeterministic agent is explicitly unstable —
no rates (§II.9 validity gate).

Findings are matched across runs by a **semantic key** (rule + finding_type + evidence *excluding*
volatile locators like step indices and call ids), because the same defect lands at different step
positions in different traces.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from tracelint.findings import ConfidenceTier, Finding, LintReport
from tracelint.rules import lint_trace
from tracelint.stats import wilson_interval
from tracelint.tools import ToolRegistry
from tracelint.trace import Trace

_VOLATILE_EVIDENCE = {"step_indices", "call_id", "consumed_values"}


def finding_key(finding: Finding) -> str:
    """A stable, position-independent identity for matching a finding across runs."""
    evidence = {k: v for k, v in finding.evidence.items() if k not in _VOLATILE_EVIDENCE}
    return json.dumps([finding.rule, finding.finding_type, evidence], sort_keys=True, default=str)


@dataclass
class FindingReproduction:
    """How reliably one finding reproduced across ``runs`` runs of the same scenario."""

    rule: str
    finding_type: str
    tier: ConfidenceTier
    summary: str
    count: int
    runs: int
    rate: float
    ci: tuple[float, float]
    flaky: bool


@dataclass
class ReproductionReport:
    """Aggregated findings across ``k`` runs, with reproduction rates and flaky flags."""

    runs: int
    findings: list[FindingReproduction] = field(default_factory=list)

    @property
    def stable(self) -> bool:
        """False for ``k < 2`` — a single run of a nondeterministic agent gives no rates."""
        return self.runs >= 2

    @property
    def flaky(self) -> list[FindingReproduction]:
        return [f for f in self.findings if f.flaky]

    @property
    def consistent(self) -> list[FindingReproduction]:
        return [f for f in self.findings if f.count == self.runs]


def aggregate_runs(reports: list[LintReport]) -> ReproductionReport:
    """Aggregate per-run lint reports of the *same* scenario into reproduction rates."""
    runs = len(reports)
    presence: dict[str, dict] = {}
    for report in reports:
        seen: set[str] = set()
        for finding in report.active_findings:
            key = finding_key(finding)
            if key in seen:
                continue  # count a finding once per run
            seen.add(key)
            slot = presence.setdefault(key, {"count": 0, "example": finding})
            slot["count"] += 1

    findings: list[FindingReproduction] = []
    for slot in presence.values():
        count = slot["count"]
        example: Finding = slot["example"]
        findings.append(
            FindingReproduction(
                rule=example.rule,
                finding_type=example.finding_type,
                tier=example.tier,
                summary=example.summary,
                count=count,
                runs=runs,
                rate=count / runs if runs else 0.0,
                ci=wilson_interval(count, runs),
                flaky=0 < count < runs,
            )
        )
    findings.sort(key=lambda f: (-f.count, f.rule, f.finding_type))
    return ReproductionReport(runs=runs, findings=findings)


def lint_runs(
    traces: list[Trace], rules: list, registry: ToolRegistry | None = None
) -> list[LintReport]:
    """Lint several traces (repeated runs of one scenario) with the same rules and registry."""
    registry = registry or ToolRegistry()
    return [lint_trace(trace, rules, registry) for trace in traces]
