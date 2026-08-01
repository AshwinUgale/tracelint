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

from tracelint.findings import Finding, LintReport
from tracelint.tools import ToolRegistry
from tracelint.trace import Trace


class Rule(ABC):
    """Base class for every deterministic check.

    Subclasses set ``id`` and ``finding_type`` and implement :meth:`run`. They may override
    :meth:`applicable` to declare what they need from a trace; the default is "always runnable".
    """

    #: Short rule id, e.g. ``"R1"``.
    id: str = ""
    #: The semantic kind this rule emits, used to label suppression records.
    finding_type: str = ""

    def applicable(self, trace: Trace, registry: ToolRegistry) -> str | None:
        """Return ``None`` if runnable, else a reason this rule is suppressed on ``trace``."""
        return None

    @abstractmethod
    def run(self, trace: Trace, registry: ToolRegistry) -> list[Finding]:
        """Produce findings for ``trace``. Called only when :meth:`applicable` returned ``None``."""
        raise NotImplementedError


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
    registry = registry or ToolRegistry()
    findings: list[Finding] = []
    for rule in rules:
        reason = rule.applicable(trace, registry)
        if reason is not None:
            findings.append(Finding.suppressed(rule.id, rule.finding_type, reason))
            continue
        findings.extend(rule.run(trace, registry))
    return LintReport(run_id=trace.run_id, findings=findings)
