"""The uniform finding shape and the lint report (spec §II.4).

Every rule — deterministic or heuristic — emits the *same* ``Finding`` shape, so a report can
list them uniformly and a CI gate can reason about them without special cases. Two axes are kept
deliberately **orthogonal** (spec §II.4):

- ``confidence_tier`` — *how sure* we are:
    - ``hard_event``  : a structurally-certain fact happened (e.g. a tool returned HTTP 500).
    - ``hard_defect`` : a structurally-provable defect (e.g. args violate the tool schema).
    - ``candidate``   : a heuristic signal for human review, shown *with its evidence*, never
                        asserted as a verdict (deep-design principle: "candidate, not verdict").
- ``finding_type`` — *what kind* of thing it is (``schema_violation``, ``tool_error_event``,
  ``hallucinated_arg``, ``loop``, ``redundant_call``, ...). A single kind can appear at more than
  one tier: a ``tool_error_event`` is a ``hard_event`` from a structured status field but a
  ``candidate`` from an exception-like string in free-form content.

A **suppression** is also a ``Finding`` (with ``suppressed_reason`` set and no evidence): when a
rule cannot run because the trace lacks a field it needs, that absence is recorded and disclosed,
never silently treated as a clean bill of health (deep-design Trap 1 / spec §II.9 fail-closed).

An **ignored** finding is one the project's configuration accepted, with a reason
(``ignored_reason``). It stays in the report, shown and counted, but no longer counts toward the
exit code.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

#: ``evidence["cause"]`` of a suppression caused by call arguments the trace did not record — a gap
#: in the record that a ``tools.json`` cannot fill (unlike a missing schema or contract).
ARGS_UNKNOWN = "arguments_unknown"


class ConfidenceTier(str, Enum):
    """How much to trust a finding. See module docstring."""

    HARD_EVENT = "hard_event"
    HARD_DEFECT = "hard_defect"
    CANDIDATE = "candidate"

    @property
    def rank(self) -> int:
        """Severity order for a CI gate: candidate < hard_event < hard_defect."""
        return _RANK[self]


_RANK = {ConfidenceTier.CANDIDATE: 0, ConfidenceTier.HARD_EVENT: 1, ConfidenceTier.HARD_DEFECT: 2}


@dataclass
class Finding:
    """One structural observation about a trace (spec §II.4).

    - ``rule``: the rule id that produced it (e.g. ``"R1"``).
    - ``finding_type``: the semantic kind (see module docstring).
    - ``tier``: the confidence tier.
    - ``summary``: a one-line human-readable statement of what was found.
    - ``evidence``: supporting data — by convention includes ``step_indices`` (the exact trace
      locations) plus rule-specific detail — so a ``candidate`` can be reviewed, not just trusted.
    - ``possible_false_positive``: set when the rule knows a legitimate pattern could trip it
      (a real retry loop, a generated idempotency key), signalling extra caution to the reader.
    - ``suppressed_reason``: when set, this is a *suppression* record, not a defect — the named
      rule could not run because the trace was missing something, and that is disclosed here.
    - ``ignored_reason``: when set, the project's configuration accepted this finding for the given
      reason; it is still reported, but no longer counts toward the exit code.
    """

    rule: str
    finding_type: str
    tier: ConfidenceTier
    summary: str
    evidence: dict[str, Any] = field(default_factory=dict)
    possible_false_positive: bool = False
    suppressed_reason: str | None = None
    ignored_reason: str | None = None

    @property
    def is_suppression(self) -> bool:
        return self.suppressed_reason is not None

    @property
    def is_ignored(self) -> bool:
        return self.ignored_reason is not None

    @property
    def step_indices(self) -> list[int]:
        idx = self.evidence.get("step_indices", [])
        return list(idx) if isinstance(idx, (list, tuple)) else []

    @classmethod
    def suppressed(cls, rule: str, finding_type: str, reason: str) -> Finding:
        """Build a suppression record for a rule that could not run on this trace."""
        return cls(
            rule=rule,
            finding_type=finding_type,
            tier=ConfidenceTier.CANDIDATE,
            summary=f"rule {rule} suppressed: {reason}",
            suppressed_reason=reason,
        )

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "rule": self.rule,
            "finding_type": self.finding_type,
            "tier": self.tier.value,
            "summary": self.summary,
            "evidence": self.evidence,
            "possible_false_positive": self.possible_false_positive,
        }
        if self.suppressed_reason is not None:
            out["suppressed_reason"] = self.suppressed_reason
        if self.ignored_reason is not None:
            out["ignored_reason"] = self.ignored_reason
        return out

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Finding:
        return cls(
            rule=data["rule"],
            finding_type=data["finding_type"],
            tier=ConfidenceTier(data["tier"]),
            summary=data.get("summary", ""),
            evidence=data.get("evidence") or {},
            possible_false_positive=bool(data.get("possible_false_positive", False)),
            suppressed_reason=data.get("suppressed_reason"),
            ignored_reason=data.get("ignored_reason"),
        )


# CI exit codes (spec §II.10: "exit 2 on hard_defect").
EXIT_OK = 0
EXIT_GATE = 1  # a lower gate the caller opted into (--fail-on hard_event / candidate) was hit.
EXIT_HARD_DEFECT = 2
EXIT_INPUT_ERROR = 3  # bad/missing trace or tools file, unknown rule, malformed JSON.


@dataclass(frozen=True)
class Coverage:
    """How much of a trace a rule could actually evaluate.

    ``evaluatable`` of ``total`` units (tool calls, tool results, ...) were checkable; the remainder
    the rule had to abstain on (no schema declared, an unclassifiable status, ...). This turns the
    honest question *"what portion of this run was actually verifiable?"* into a number you can
    watch across releases, and it is *why* a clean report is trustworthy — you can see how much was
    checked, not merely that nothing fired.
    """

    rule: str
    unit: str
    evaluatable: int
    total: int

    @property
    def ratio(self) -> float:
        return 1.0 if self.total == 0 else self.evaluatable / self.total

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule": self.rule,
            "unit": self.unit,
            "evaluatable": self.evaluatable,
            "total": self.total,
        }


@dataclass
class LintReport:
    """The result of linting one trace: all findings plus the run they describe.

    ``active_findings`` are real observations; ``suppressions`` are the rules that could not run;
    ``ignored`` are findings the project's configuration accepted (with a reason). ``coverage``
    records, per reporting rule, how many units it could evaluate (:class:`Coverage`).
    ``exit_code`` implements the CI contract: ``2`` on a ``hard_defect``, exactly the tier reserved
    for structurally-provable defects; ``1`` when ``fail_on`` opts into a lower tier
    (``hard_event`` or ``candidate``) and an active finding reaches it, or a ``gate_failures`` entry
    says the run checked less than its baseline; else ``0``. By default CI never fails on an event
    or a heuristic candidate.
    """

    run_id: str
    findings: list[Finding] = field(default_factory=list)
    coverage: list[Coverage] = field(default_factory=list)
    fail_on: ConfidenceTier = ConfidenceTier.HARD_DEFECT
    gate_failures: list[str] = field(default_factory=list)

    @property
    def active_findings(self) -> list[Finding]:
        return [f for f in self.findings if not f.is_suppression and not f.is_ignored]

    @property
    def ignored(self) -> list[Finding]:
        return [f for f in self.findings if f.is_ignored]

    @property
    def suppressions(self) -> list[Finding]:
        return [f for f in self.findings if f.is_suppression]

    def by_tier(self, tier: ConfidenceTier) -> list[Finding]:
        return [f for f in self.active_findings if f.tier == tier]

    @property
    def has_hard_defect(self) -> bool:
        return any(f.tier == ConfidenceTier.HARD_DEFECT for f in self.active_findings)

    @property
    def exit_code(self) -> int:
        if self.has_hard_defect:
            return EXIT_HARD_DEFECT
        if self.gate_failures or any(
            f.tier.rank >= self.fail_on.rank for f in self.active_findings
        ):
            return EXIT_GATE
        return EXIT_OK

    def to_dict(self) -> dict[str, Any]:
        # ``findings`` is the active findings only, so its count matches the text report.
        # Suppressions (not checked) and ignored (accepted) are disclosed in their own arrays,
        # never folded into ``findings`` where they would inflate the count (spec II.10).
        out: dict[str, Any] = {
            "run_id": self.run_id,
            "findings": [f.to_dict() for f in self.active_findings],
            "exit_code": self.exit_code,
        }
        if self.fail_on is not ConfidenceTier.HARD_DEFECT:
            out["fail_on"] = self.fail_on.value
        if self.gate_failures:
            out["gate_failures"] = list(self.gate_failures)
        if self.ignored:
            out["ignored"] = [f.to_dict() for f in self.ignored]
        if self.suppressions:
            out["suppressions"] = [f.to_dict() for f in self.suppressions]
        if self.coverage:
            out["coverage"] = [c.to_dict() for c in self.coverage]
        return out
