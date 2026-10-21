"""Inspect Scout integration — tracelint's deterministic rules as Scout scanners.

Inspect Scout (https://meridianlabs-ai.github.io/inspect_scout) scans agent transcripts — Inspect
eval logs, and the traces it imports from Phoenix, LangSmith, Logfire, Weave, MLflow, Claude Code
and Harbor — with *scanners*: functions that read one transcript and return results that cite the
messages they are about. This package exposes tracelint's rules as such scanners, so a Scout user
gets reference-free trajectory checks (tool errors, loops, redundant and malformed calls, and with a
contract the schema and integrity rules) beside their other scanners, with no model call.

Install with ``pip install "tracelint[scout]"``. The scanners register under the ``tracelint``
namespace through the ``inspect_ai`` entry point, so the Scout CLI finds them by name::

    scout scan tracelint/tracelint -T ./logs
    scout scan tracelint/tracelint -T ./logs -S tools=tools.json

Scanners (see :mod:`tracelint.integrations.scout.scanners`; the rule scanners take ``tools`` and
``min_tier``):

- ``tracelint`` — every selected rule in one pass. One result per rule, labelled with the rule's
  finding type (``tool_error_event``, ``loop``, ...), like Scout's ``grep_scanner`` with a dict of
  patterns. ``rules`` picks the rules; by default the ones that need no contract (R2a, R4, R5, R6),
  plus the contract rules (R1, R7, R8, R9, R10, R12, and R13 when it declares ``off_limits``) when a
  ``tools`` contract is given.
- ``tool_errors`` (R2a), ``tool_loops`` (R4), ``redundant_calls`` (R5), ``malformed_arguments``
  (R6) — one rule each, one result.
- ``record_quality`` — not a rule: whether the transcript records the run completely enough to
  trust (:mod:`tracelint.record`). One result whose ``value`` is the record's issue codes, ``[]``
  when there are none (:func:`check_messages`).

Each rule result's ``value`` is the number of findings at or above ``min_tier`` (default: every
tier), ``0`` when there were none, and ``None`` when the rule could not check the transcript (the
contract lacks what it needs, or the transcript did not record it) — never a silent clean pass. The
explanation lists each finding with the messages it cites (``[M3]``, numbered like Scout's own
``grep_scanner``), ``references`` link those messages, and ``metadata`` carries the rule id, the
count per tier, each finding's evidence, coverage and what went unchecked.

This module is the pure core and needs no Scout install; :mod:`.scanners` wraps it in Scout's
``@scanner`` decorator.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tracelint.adapters.inspect import inspect_trace, subagent_calls
from tracelint.findings import SUPPRESS_NOT_APPLICABLE, ConfidenceTier, Coverage, Finding
from tracelint.record import PLACEHOLDER, SUBAGENT_LAUNCH, check_record, summarize
from tracelint.rules import Rule, lint_trace, select_rules
from tracelint.tools import ToolRegistry
from tracelint.trace import Role, ToolCall, Trace

#: Rules that need nothing but the transcript.
KEYLESS_RULES = ("R2a", "R4", "R5", "R6")
#: Rules that check the transcript against a ``tools.json`` contract.
CONTRACT_RULES = ("R1", "R7", "R8", "R9", "R10", "R12", "R13")

_EXPLANATION_FINDINGS = 25  # findings listed in an explanation; metadata keeps every one


@dataclass(frozen=True)
class Citation:
    """A message a finding is about: its position in the transcript and its id."""

    position: int
    message_id: str | None

    @property
    def cite(self) -> str:
        """Scout's citation for the message (``[M1]`` is the first message)."""
        return f"[M{self.position + 1}]"


@dataclass
class RuleResult:
    """One rule's outcome on one transcript, in the shape of a Scout ``Result``."""

    rule: str
    label: str
    value: int | None
    explanation: str
    citations: list[Citation] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


Tools = str | Path | dict[str, Any] | ToolRegistry | None


def resolve_contract(tools: Tools) -> ToolRegistry | None:
    """The contract as a :class:`ToolRegistry`: a ``tools.json`` path (``tracelint init`` and
    ``init --from-mcp`` write one), its parsed dict, a registry as-is, or ``None`` for none."""
    if tools is None or isinstance(tools, ToolRegistry):
        return tools
    if isinstance(tools, dict):
        return ToolRegistry.from_dict(tools)
    if isinstance(tools, (str, Path)):
        return ToolRegistry.load(tools)
    raise TypeError(
        f"tools must be a tools.json path, a dict, or a ToolRegistry, not {type(tools).__name__}"
    )


def resolve_rules(rules: str | Iterable[str] | None, registry: ToolRegistry | None) -> list[str]:
    """The rule ids to run. ``rules`` may be a list or a comma-separated string (``-S rules=R4,R5``
    arrives as either). Without it: the keyless rules, plus the contract rules when there is a
    contract — R13 only when that contract declares ``off_limits``, since it has nothing to check
    otherwise."""
    if isinstance(rules, str):
        rules = [r.strip() for r in rules.split(",") if r.strip()]
    if rules:
        chosen = list(dict.fromkeys(rules))
        select_rules(chosen)  # raises on an unknown id, naming the known ones
        return chosen
    chosen = list(KEYLESS_RULES)
    if registry is not None:
        chosen += [r for r in CONTRACT_RULES if r != "R13" or registry.off_limits is not None]
    return chosen


def _tier(min_tier: str | ConfidenceTier) -> ConfidenceTier:
    return min_tier if isinstance(min_tier, ConfidenceTier) else ConfidenceTier(min_tier)


def _citations(finding: Finding, trace: Trace, positions: list[int]) -> list[Citation]:
    """The messages a finding's steps came from, in transcript order."""
    seen: dict[int, Citation] = {}
    for i in finding.step_indices:
        if 0 <= i < len(positions) and positions[i] not in seen:
            source = trace.steps[i].source
            seen[positions[i]] = Citation(positions[i], source.observation_id if source else None)
    return [seen[p] for p in sorted(seen)]


def _coverage_text(coverage: Coverage | None) -> str:
    if coverage is None:
        return ""
    if coverage.evaluatable == coverage.total:
        return f" ({coverage.total} {coverage.unit} checked)"
    return f" ({coverage.evaluatable} of {coverage.total} {coverage.unit} could be checked)"


def _rule_result(
    rule: Rule,
    findings: list[Finding],
    coverage: Coverage | None,
    *,
    trace: Trace,
    positions: list[int],
    registry: ToolRegistry | None,
    min_tier: ConfidenceTier,
) -> RuleResult:
    """One rule's findings on the transcript as a :class:`RuleResult`."""
    active = [
        f
        for f in findings
        if not f.is_suppression and not f.is_ignored and f.tier.rank >= min_tier.rank
    ]
    suppressions = [f for f in findings if f.is_suppression]
    # A suppression without steps is the whole rule abstaining (``applicable``); one with steps
    # covers only the calls it names, and the rest of the transcript was checked.
    whole = [f for f in suppressions if not f.step_indices]
    partial = [f for f in suppressions if f.step_indices]
    # The same keys on every result: Scout spreads a result set's metadata into columns.
    metadata: dict[str, Any] = {
        "rule": rule.id,
        "tiers": dict(Counter(f.tier.value for f in active)),
        "findings": [],
        "coverage": coverage.to_dict() if coverage is not None else None,
        "not_checked": [],
    }

    if rule.id == "R13" and (registry is None or registry.off_limits is None):
        reason = "the contract declares no off_limits sources"
        metadata["not_checked"] = [reason]
        return RuleResult(rule.id, rule.finding_type, None, f"Not checked: {reason}.", [], metadata)
    if whole:
        reasons = [f.suppressed_reason or "" for f in whole]
        metadata["not_checked"] = reasons
        # Nothing the rule could find here (too few calls, no results) is a true zero; a missing
        # contract fact or unrecorded data is not a clean pass.
        if all(f.suppressed_category == SUPPRESS_NOT_APPLICABLE for f in whole):
            value, prefix = 0, "Nothing to check"
        else:
            value, prefix = None, "Not checked"
        explanation = f"{prefix}: {'; '.join(reasons)}."
        return RuleResult(rule.id, rule.finding_type, value, explanation, [], metadata)

    citations: dict[int, Citation] = {}
    lines: list[str] = []
    for finding in active:
        cited = _citations(finding, trace, positions)
        for c in cited:
            citations.setdefault(c.position, c)
        entry = finding.to_dict()
        entry["messages"] = [c.cite for c in cited]
        metadata["findings"].append(entry)
        if len(lines) < _EXPLANATION_FINDINGS:
            where = " ".join(c.cite for c in cited)
            lines.append(f"{where + ' ' if where else ''}({finding.tier.value}) {finding.summary}")
    if len(active) > _EXPLANATION_FINDINGS:
        lines.append(f"... and {len(active) - _EXPLANATION_FINDINGS} more (see metadata)")
    if partial:
        metadata["not_checked"] = [f.suppressed_reason or "" for f in partial]
        lines.extend(f"Not checked: {f.suppressed_reason}" for f in partial)
    if not active:
        kind = rule.finding_type.replace("_", " ")
        lines.insert(0, f"No {kind} found{_coverage_text(coverage)}.")
    return RuleResult(
        rule.id,
        rule.finding_type,
        len(active),
        "\n".join(lines),
        [citations[p] for p in sorted(citations)],
        metadata,
    )


def lint_messages(
    messages: Iterable[Any],
    *,
    events: Iterable[Any] | None = None,
    rules: str | Iterable[str] | None = None,
    tools: Tools = None,
    min_tier: str | ConfidenceTier = ConfidenceTier.CANDIDATE,
    run_id: str = "inspect-run",
) -> list[RuleResult]:
    """Lint a transcript's Inspect chat messages; one :class:`RuleResult` per rule, in rule order.

    ``events`` are the transcript's (``tool``) events, which hold the failures Scout's importers
    record; ``rules`` / ``tools`` / ``min_tier`` are the scanners' arguments (module docstring).
    Needs no Scout install: messages and events may be Inspect's objects or their JSON form.
    """
    registry = resolve_contract(tools)
    floor = _tier(min_tier)
    chosen = select_rules(resolve_rules(rules, registry))
    trace, positions = inspect_trace(messages, events=events, run_id=run_id)
    report = lint_trace(trace, chosen, registry)
    coverage = {c.rule: c for c in report.coverage}
    return [
        _rule_result(
            rule,
            [f for f in report.findings if f.rule == rule.id],
            coverage.get(rule.id),
            trace=trace,
            positions=positions,
            registry=registry,
            min_tier=floor,
        )
        for rule in chosen
    ]


# --- Record quality -----------------------------------------------------------------------------

_RECORD_CITES = 10  # messages cited per record issue; the counts are in metadata


@dataclass
class RecordResult:
    """A transcript's record quality, in the shape of a Scout ``Result``."""

    value: list[str]
    explanation: str
    citations: list[Citation] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


def _record_steps(trace: Trace, recorded_subagents: set[str]) -> dict[str, list[int]]:
    """The steps behind each record issue that points at particular steps, for citations."""
    calls = trace.tool_calls()
    seen: set[str] = set()
    duplicates = []
    for call in calls:
        if call.call_id in seen:
            duplicates.append(call.index)
        seen.add(call.call_id)
    last = trace.steps[-1] if trace.steps else None
    return {
        "results_missing": [c.index for c in calls if trace.result_for(c) is None],
        "arguments_missing": [c.index for c in calls if c.args_unavailable is not None],
        "placeholder_turns": [
            m.index
            for m in trace.messages()
            if m.role is Role.ASSISTANT and PLACEHOLDER.match(m.content or "")
        ],
        "ends_unanswered": [last.index] if isinstance(last, ToolCall) else [],
        "subagents_missing": [
            c.index
            for c in calls
            if SUBAGENT_LAUNCH.search(c.name or "") and c.call_id not in recorded_subagents
        ],
        "duplicate_call_ids": duplicates,
    }


def check_messages(
    messages: Iterable[Any],
    *,
    events: Iterable[Any] | None = None,
    run_id: str = "inspect-run",
) -> RecordResult:
    """The record quality of a transcript (:mod:`tracelint.record`): is it complete enough for
    the rules, or a judge, to trust? It judges the record, never the agent.

    ``value`` is the codes of the record's issues (``results_missing``, ``ends_unanswered``, ...),
    ``[]`` when there are none — the shape of a Scout multi-label answer. ``events`` are the
    transcript's ``tool`` and ``span_begin`` events: a failure recorded there is an outcome, and a
    subagent whose run Scout kept as an ``agent`` span is a recorded run, not a missing one.
    """
    events = list(events or ())
    trace, positions = inspect_trace(messages, events=events, run_id=run_id)
    recorded = subagent_calls(events)
    launches = [c for c in trace.tool_calls() if SUBAGENT_LAUNCH.search(c.name or "")]
    check = check_record(trace, subagent_runs=sum(c.call_id in recorded for c in launches))
    issues = check.record_issues()
    steps = _record_steps(trace, recorded)

    citations: dict[int, Citation] = {}
    lines = []
    for issue in issues:
        cited: dict[int, Citation] = {}
        for i in steps.get(issue.code, []):
            if positions[i] not in cited:
                source = trace.steps[i].source
                cited[positions[i]] = Citation(
                    positions[i], source.observation_id if source else None
                )
        shown = [cited[p] for p in sorted(cited)][:_RECORD_CITES]
        for c in shown:
            citations.setdefault(c.position, c)
        where = " ".join(c.cite for c in shown) + (" …" if len(cited) > len(shown) else "")
        heuristic = " (heuristic)" if issue.heuristic else ""
        lines.append(f"{issue.text}{heuristic}{': ' + where if where else ''}")
    if not lines:
        lines.append(
            f"No record issues found ({check.calls} tool calls, {check.results_recorded} with a "
            "result)."
        )
    metadata = {
        **check.to_dict(),
        "coverage": summarize([check]).coverage(),
        "heuristic": [i.code for i in issues if i.heuristic],
    }
    return RecordResult(
        [i.code for i in issues],
        "\n".join(lines),
        [citations[p] for p in sorted(citations)],
        metadata,
    )


__all__ = [
    "CONTRACT_RULES",
    "KEYLESS_RULES",
    "Citation",
    "RecordResult",
    "RuleResult",
    "check_messages",
    "lint_messages",
    "resolve_contract",
    "resolve_rules",
]
