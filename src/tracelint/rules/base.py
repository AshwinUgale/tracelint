"""The rule contract and the fail-closed driver (spec §II.4, §II.9).

The single most important property of this tool is honesty about *what it could not check*
(deep-design Trap 1). A rule therefore has two methods:

- ``applicable(trace, registry)`` returns ``None`` if the rule can run, or a short **reason
  string** if it cannot (a field it needs is missing, the required tool schema is absent, the
  trace is not stage-decomposable, ...). This is the fail-closed gate.
- ``run(trace, registry)`` produces findings, and is called **only** when ``applicable``
  returned ``None``.

:func:`lint_trace` wires them together: for each rule it either records a **suppression** (with
the stated reason) or runs the rule and collects its findings. A suppressed rule is disclosed in
the report, never silently skipped — a clean report with hidden suppressions would be the exact
"false confidence" failure the whole design exists to avoid.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable
from typing import NamedTuple

from tracelint.findings import (
    ARGS_UNKNOWN,
    RESULT_UNRECORDED,
    SUPPRESS_NEEDS_CONTRACT,
    SUPPRESS_NOT_APPLICABLE,
    SUPPRESS_NOT_RECORDED,
    ConfidenceTier,
    Coverage,
    Finding,
    LintReport,
)
from tracelint.tools import ToolRegistry
from tracelint.trace import ToolCall, Trace


class Suppression(NamedTuple):
    """A reason a rule abstained, with its category (see ``findings`` ``SUPPRESS_*``)."""

    reason: str
    category: str


class Rule(ABC):
    """Base class for every deterministic check.

    Subclasses set ``id`` and ``finding_type`` and implement :meth:`run`. They may override
    :meth:`applicable` to declare what they need from a trace; the default is "always runnable".
    """

    #: Short rule id, e.g. ``"R1"``.
    id: str = ""
    #: The semantic kind this rule emits, used to label suppression records.
    finding_type: str = ""

    def applicable(self, trace: Trace, registry: ToolRegistry) -> Suppression | str | None:
        """Return ``None`` if runnable, else a :class:`Suppression` (reason + category), or a
        bare reason string (taken as an uncategorised suppression)."""
        return None

    def not_applicable(self, reason: str) -> Suppression:
        """This trace cannot trigger the rule (too few calls, no results). Nothing to check."""
        return Suppression(reason, SUPPRESS_NOT_APPLICABLE)

    def needs_contract(self, reason: str) -> Suppression:
        """A ``tools.json`` fact is missing (schema, failure_when, registry); ``init`` helps."""
        return Suppression(reason, SUPPRESS_NEEDS_CONTRACT)

    def not_recorded(self, reason: str) -> Suppression:
        """The trace did not capture the data the rule needs; no ``tools.json`` fills that gap."""
        return Suppression(reason, SUPPRESS_NOT_RECORDED)

    def coverage(self, trace: Trace, registry: ToolRegistry) -> Coverage | None:
        """How many of this rule's units it could actually evaluate, or ``None`` to not report.

        Optional. A rule with a natural unit (tool calls for R1, tool results for R2) reports how
        many were checkable vs. abstained on, so a reader can trust *how much* was verified — not
        just that nothing fired. Whole-rule suppression shows up here as ``0 / total``.
        """
        return None

    @abstractmethod
    def run(self, trace: Trace, registry: ToolRegistry) -> list[Finding]:
        """Produce findings for ``trace``. Called only when :meth:`applicable` returned ``None``."""
        raise NotImplementedError

    def unknown_args_suppression(self, calls: Iterable[ToolCall], checked: str) -> Finding | None:
        """One suppression for the ``calls`` this rule could not check because the trace did not
        record their real arguments (:attr:`ToolCall.args_unavailable`), or ``None`` if none.

        A rule that compares or traces argument values must skip such calls, and skipping them
        silently would read as a clean pass. Pass only the calls where a finding was otherwise
        possible; ``checked`` names what went unchecked (e.g. ``"duplicate side effects"``).
        """
        unknown = list({c.index: c for c in calls if c.args_unavailable is not None}.values())
        if not unknown:
            return None
        unknown.sort(key=lambda c: c.index)
        tools = sorted({c.name for c in unknown})
        reasons = list(dict.fromkeys(str(c.args_unavailable) for c in unknown))
        more = f" (+{len(reasons) - 1} other reason(s))" if len(reasons) > 1 else ""
        n = len(unknown)
        reason = (
            f"{n} call{'s' if n != 1 else ''} to {', '.join(repr(t) for t in tools)} not checked "
            f"for {checked} — arguments unknown: {reasons[0]}{more}"
        )
        return Finding(
            rule=self.id,
            finding_type=self.finding_type,
            tier=ConfidenceTier.CANDIDATE,
            summary=f"rule {self.id} suppressed for {n} call{'s' if n != 1 else ''}: {reason}",
            evidence={
                "step_indices": [c.index for c in unknown],
                "tools": tools,
                "cause": ARGS_UNKNOWN,
            },
            suppressed_reason=reason,
            suppressed_category=SUPPRESS_NOT_RECORDED,
        )

    def unrecorded_result_suppression(
        self, calls: Iterable[ToolCall], checked: str
    ) -> Finding | None:
        """One suppression for the ``calls`` this rule could not check because the trace recorded
        no result for them, or ``None`` if none.

        The counterpart of :meth:`unknown_args_suppression` for results: a missing result is
        unknown, so it can't be shown equal to another call's — and skipping the call silently would
        read as a clean pass. Pass only the calls where a finding was otherwise possible.
        """
        missing = sorted({c.index: c for c in calls}.values(), key=lambda c: c.index)
        if not missing:
            return None
        tools = sorted({c.name for c in missing})
        n = len(missing)
        reason = (
            f"{n} call{'s' if n != 1 else ''} to {', '.join(repr(t) for t in tools)} not checked "
            f"for {checked} — no result was recorded for them (e.g. an earlier call in a batch "
            "that returns one observation, or a server-side tool)"
        )
        return Finding(
            rule=self.id,
            finding_type=self.finding_type,
            tier=ConfidenceTier.CANDIDATE,
            summary=f"rule {self.id} suppressed for {n} call{'s' if n != 1 else ''}: {reason}",
            evidence={
                "step_indices": [c.index for c in missing],
                "tools": tools,
                "cause": RESULT_UNRECORDED,
            },
            suppressed_reason=reason,
            suppressed_category=SUPPRESS_NOT_RECORDED,
        )


def lint_trace(
    trace: Trace,
    rules: list[Rule],
    registry: ToolRegistry | None = None,
) -> LintReport:
    """Run ``rules`` over ``trace``, recording suppressions for any rule that cannot run.

    Order is preserved so a report reads in rule order. A rule that raises is *not* swallowed —
    a crashing rule is a bug in the linter, not a finding about the trace, and hiding it would
    undermine the tool's credibility.
    """
    registry = registry if registry is not None else ToolRegistry()
    findings: list[Finding] = []
    coverage: list[Coverage] = []
    for rule in rules:
        cov = rule.coverage(trace, registry)
        if cov is not None:
            coverage.append(cov)
        verdict = rule.applicable(trace, registry)
        if verdict is not None:
            reason = verdict.reason if isinstance(verdict, Suppression) else verdict
            category = verdict.category if isinstance(verdict, Suppression) else None
            findings.append(
                Finding.suppressed(rule.id, rule.finding_type, reason, category=category)
            )
            continue
        findings.extend(rule.run(trace, registry))
    return LintReport(run_id=trace.run_id, findings=findings, coverage=coverage)
