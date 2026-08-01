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
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ConfidenceTier(str, Enum):
    """How much to trust a finding. See module docstring."""

    HARD_EVENT = "hard_event"
    HARD_DEFECT = "hard_defect"
    CANDIDATE = "candidate"


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
    """

    rule: str
    finding_type: str
    tier: ConfidenceTier
    summary: str
    evidence: dict[str, Any] = field(default_factory=dict)
    possible_false_positive: bool = False
    suppressed_reason: str | None = None

    @property
    def is_suppression(self) -> bool:
        return self.suppressed_reason is not None

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
        )


# CI exit codes (spec §II.10: "exit 2 on hard_defect").
EXIT_OK = 0
EXIT_GATE = 1  # reserved: a configured gate (e.g. --fail-on candidate) was tripped.
EXIT_HARD_DEFECT = 2
EXIT_INPUT_ERROR = 3  # bad/missing trace or tools file, unknown rule, malformed JSON.


@dataclass
class LintReport:
    """The result of linting one trace: all findings plus the run they describe.

    ``active_findings`` are real observations; ``suppressions`` are the rules that could not run.
    ``exit_code`` implements the CI contract — a non-zero exit is driven by a ``hard_defect``,
    exactly the tier reserved for structurally-provable defects, so CI never fails on a heuristic
    candidate unless a caller explicitly opts in later.
    """

    run_id: str
    findings: list[Finding] = field(default_factory=list)

    @property
    def active_findings(self) -> list[Finding]:
        return [f for f in self.findings if not f.is_suppression]

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
        return EXIT_HARD_DEFECT if self.has_hard_defect else EXIT_OK

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "findings": [f.to_dict() for f in self.findings],
            "exit_code": self.exit_code,
        }
