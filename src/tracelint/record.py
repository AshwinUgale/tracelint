"""Record quality: is the trace itself complete enough to trust?

Rules judge what the agent did; these checks judge the record of it. A dashboard, a judge or a rule
downstream inherits every gap silently — a tool call whose result was never recorded looks like a
call that returned nothing, a placeholder where the reasoning should be looks like an agent that
said nothing. Measured on the Terminal-Bench 2.0 leaderboard (10,541 runs, 13 harnesses), only 67%
of tool calls had a recorded result, 2% of results said whether they succeeded, and five harnesses
wrote boilerplate such as ``Executed exec_command call_…`` for 41–81% of the agent's turns.

Each check is computed on the canonical :class:`~tracelint.trace.Trace`, so it applies to every
input format. Nothing here counts against the agent; ``tracelint doctor`` reports it, advisory
unless a ``--fail-under`` threshold is set.

What the record cannot show is not guessed at: history a producer dropped without leaving a marker
(context compaction, say) leaves an internally consistent record, and no check here finds it.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from typing import Any, NamedTuple

from tracelint.trace import Message, ResultStatus, Role, ToolCall, Trace

#: Assistant text that stands in for the turn instead of recording it, e.g. ``Executed
#: exec_command call_x`` or ``Tool call``. A heuristic: the output says so.
PLACEHOLDER = re.compile(
    r"^\s*(Executed|Tool call|Calling tool|Running tool)\b[^\n]{0,120}$", re.IGNORECASE
)
#: A value a redaction filter replaced. Analysis of that value (or of a number it overwrote) is
#: unreliable; a filter replacing a short value can also break the surrounding JSON.
REDACTION = re.compile(r"\[REDACTED[^\]\n]{0,40}\]|<REDACTED[^>\n]{0,40}>")
#: A tool that launches a subagent (``run_subagent``, ``spawn_agent``, Claude Code's ``Task`` /
#: ``Agent``), not one that reads a running subagent's output. A heuristic: by name.
SUBAGENT_LAUNCH = re.compile(
    r"^(task|agent)$|(run|spawn|launch|start|create|call|invoke)_?(sub_?)?agent|delegate",
    re.IGNORECASE,
)

#: The coverage measures ``--fail-under`` accepts, as ``name=percent``.
MEASURES = ("results", "outcome", "arguments", "reasoning")


class RecordIssue(NamedTuple):
    """One thing wrong with a record: a stable ``code``, the phrase ``doctor`` prints, and whether
    it rests on a heuristic (placeholder text, a tool's name) rather than on the record's
    structure."""

    code: str
    text: str
    heuristic: bool = False


@dataclass
class RecordCheck:
    """The record quality of one trace."""

    run_id: str
    model: str | None = None
    group: str | None = None
    steps: int = 0
    calls: int = 0
    results_recorded: int = 0  # calls with a recorded result
    outcome_recorded: int = 0  # recorded results that say whether they succeeded
    arguments_recorded: int = 0  # calls whose arguments were recorded
    assistant_turns: int = 0
    placeholder_turns: int = 0
    empty_turns: int = 0
    ends_unanswered: bool = False  # the record stops on a call with no result
    subagent_launches: int = 0
    subagent_runs: int = 0  # subagent runs recorded for this trace (``<run_id>/<child>``)
    duplicate_call_ids: int = 0
    redactions: int = 0

    @property
    def empty(self) -> bool:
        return self.steps == 0

    @property
    def reasoning_recorded(self) -> int:
        return self.assistant_turns - self.placeholder_turns - self.empty_turns

    @property
    def subagents_missing(self) -> int:
        return max(self.subagent_launches - self.subagent_runs, 0)

    def record_issues(self) -> list[RecordIssue]:
        """What is wrong with this record, each with a stable code (empty when nothing is)."""
        out = []
        if self.empty:
            out.append(RecordIssue("empty", "empty: no steps"))
        if self.calls and self.results_recorded < self.calls:
            missing = self.calls - self.results_recorded
            out.append(
                RecordIssue("results_missing", f"{missing}/{self.calls} calls have no result")
            )
        if self.calls and self.arguments_recorded < self.calls:
            missing = self.calls - self.arguments_recorded
            out.append(
                RecordIssue("arguments_missing", f"{missing}/{self.calls} calls have no arguments")
            )
        if self.placeholder_turns:
            share = f"{self.placeholder_turns}/{self.assistant_turns}"
            text = f"{share} assistant turns are placeholders"
            out.append(RecordIssue("placeholder_turns", text, heuristic=True))
        if self.ends_unanswered:
            out.append(RecordIssue("ends_unanswered", "ends on a call with no result"))
        if self.subagents_missing:
            text = f"{self.subagents_missing} subagent run(s) not recorded"
            out.append(RecordIssue("subagents_missing", text, heuristic=True))
        if self.duplicate_call_ids:
            text = f"{self.duplicate_call_ids} duplicate call id(s)"
            out.append(RecordIssue("duplicate_call_ids", text))
        if self.redactions:
            out.append(RecordIssue("redactions", f"{self.redactions} redacted value(s)"))
        return out

    def issues(self) -> list[str]:
        """What is wrong with this record, one phrase each (empty when nothing is)."""
        return [issue.text for issue in self.record_issues()]

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.update(
            empty=self.empty,
            reasoning_recorded=self.reasoning_recorded,
            subagents_missing=self.subagents_missing,
            issues=self.issues(),
        )
        return data


def check_record(trace: Trace, *, subagent_runs: int = 0, group: str | None = None) -> RecordCheck:
    """Measure one trace's record. ``subagent_runs``: how many subagent runs of this trace the
    caller has (a subagent is its own run, ``<run_id>/<child>``, so only the batch knows)."""
    steps = trace.steps
    calls = trace.tool_calls()
    results = [trace.result_for(c) for c in calls]
    ids: dict[str, int] = {}
    for c in calls:
        ids[c.call_id] = ids.get(c.call_id, 0) + 1
    assistant = [s for s in steps if isinstance(s, Message) and s.role is Role.ASSISTANT]
    last = steps[-1] if steps else None
    model = next((s.meta.model for s in steps if s.meta is not None and s.meta.model), None)
    return RecordCheck(
        run_id=trace.run_id,
        model=model,
        group=group,
        steps=len(steps),
        calls=len(calls),
        results_recorded=sum(r is not None for r in results),
        outcome_recorded=sum(
            r is not None and r.status is not ResultStatus.UNKNOWN for r in results
        ),
        arguments_recorded=sum(c.args_unavailable is None for c in calls),
        assistant_turns=len(assistant),
        placeholder_turns=sum(bool(PLACEHOLDER.match(m.content or "")) for m in assistant),
        empty_turns=sum(not (m.content or "").strip() for m in assistant),
        ends_unanswered=isinstance(last, ToolCall) and trace.result_for(last) is None,
        subagent_launches=sum(bool(SUBAGENT_LAUNCH.search(c.name or "")) for c in calls),
        subagent_runs=subagent_runs,
        duplicate_call_ids=sum(n - 1 for n in ids.values() if n > 1),
        redactions=sum(len(REDACTION.findall(_text(s))) for s in steps),
    )


def check_records(
    traces: Iterable[Trace], *, groups: Iterable[str | None] | None = None
) -> list[RecordCheck]:
    """Measure a batch, crediting each trace with the subagent runs recorded under it."""
    traces = list(traces)
    groups = list(groups) if groups is not None else [None] * len(traces)
    children: dict[str, int] = {}
    for t in traces:  # a subagent is its parent's run id plus "/<child>"
        if "/" in t.run_id:
            parent = t.run_id.rsplit("/", 1)[0]
            children[parent] = children.get(parent, 0) + 1
    return [
        check_record(t, subagent_runs=children.get(t.run_id, 0), group=g)
        for t, g in zip(traces, groups, strict=True)
    ]


@dataclass
class RecordSummary:
    """Record quality across a batch of traces (or one group of them)."""

    traces: int = 0
    calls: int = 0
    results_recorded: int = 0
    outcome_recorded: int = 0
    arguments_recorded: int = 0
    assistant_turns: int = 0
    reasoning_recorded: int = 0
    empty: int = 0
    ends_unanswered: int = 0
    subagents_missing: int = 0  # traces with at least one unrecorded subagent run
    duplicate_call_ids: int = 0  # traces with duplicate call ids
    redacted: int = 0  # traces with redacted values
    unreadable: list[str] = field(default_factory=list)  # files that could not be read

    def coverage(self) -> dict[str, float | None]:
        """Each measure as a percentage; ``None`` when nothing was there to measure."""

        def pct(part: int, whole: int) -> float | None:
            return round(100 * part / whole, 1) if whole else None

        return {
            "results": pct(self.results_recorded, self.calls),
            "outcome": pct(self.outcome_recorded, self.results_recorded),
            "arguments": pct(self.arguments_recorded, self.calls),
            "reasoning": pct(self.reasoning_recorded, self.assistant_turns),
        }

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "coverage": self.coverage()}


def summarize(checks: Iterable[RecordCheck], *, unreadable: Iterable[str] = ()) -> RecordSummary:
    s = RecordSummary(unreadable=list(unreadable))
    for c in checks:
        s.traces += 1
        s.calls += c.calls
        s.results_recorded += c.results_recorded
        s.outcome_recorded += c.outcome_recorded
        s.arguments_recorded += c.arguments_recorded
        s.assistant_turns += c.assistant_turns
        s.reasoning_recorded += c.reasoning_recorded
        s.empty += c.empty
        s.ends_unanswered += c.ends_unanswered
        s.subagents_missing += c.subagents_missing > 0
        s.duplicate_call_ids += c.duplicate_call_ids > 0
        s.redacted += c.redactions > 0
    return s


def parse_thresholds(specs: Iterable[str]) -> dict[str, float]:
    """``["results=90", "outcome=50"]`` -> ``{"results": 90.0, "outcome": 50.0}``."""
    out: dict[str, float] = {}
    for spec in specs:
        name, sep, value = spec.partition("=")
        name = name.strip().lower()
        if not sep or name not in MEASURES:
            raise ValueError(
                f"--fail-under takes NAME=PERCENT with NAME one of {', '.join(MEASURES)}"
                f" (got {spec!r})"
            )
        try:
            out[name] = float(value)
        except ValueError:
            raise ValueError(f"--fail-under {spec!r}: {value!r} is not a number") from None
    return out


def below_thresholds(summary: RecordSummary, thresholds: dict[str, float]) -> list[str]:
    """The measures under their threshold, as ``"results 67.0% < 90%"`` (none measured passes)."""
    cov = summary.coverage()
    return [
        f"{name} {cov[name]}% < {limit:g}%"
        for name, limit in thresholds.items()
        if cov[name] is not None and cov[name] < limit
    ]


_HINTS = {
    "results": "record each tool call's result, paired to its call id",
    "outcome": "record whether each tool result succeeded (an is_error flag, a status or an exit "
    "code), so errors are read from a signal rather than guessed from text",
    "arguments": "record each tool call's arguments",
    "reasoning": "record the agent's own text or reasoning, not a placeholder line",
}


def render_record_quality(
    summary: RecordSummary, groups: dict[str, RecordSummary] | None = None
) -> str:
    """The record-quality section of ``tracelint doctor``."""
    cov = summary.coverage()
    lines = [f"Record quality ({summary.traces} trace(s), {summary.calls} tool call(s)):"]

    def row(label: str, value: float | None, detail: str) -> None:
        shown = "   n/a" if value is None else f"{value:5.1f}%"
        lines.append(f"  {label:<22}{shown}  {detail}")

    row("results recorded", cov["results"], f"{summary.results_recorded}/{summary.calls} calls")
    row(
        "outcome recorded",
        cov["outcome"],
        f"{summary.outcome_recorded}/{summary.results_recorded} results say success or failure",
    )
    row(
        "arguments recorded",
        cov["arguments"],
        f"{summary.arguments_recorded}/{summary.calls} calls",
    )
    row(
        "reasoning recorded",
        cov["reasoning"],
        f"{summary.reasoning_recorded}/{summary.assistant_turns} assistant turns "
        f"(heuristic: placeholder or empty turns excluded)",
    )
    counts = [
        ("ends on an unanswered call", summary.ends_unanswered, "trace(s)"),
        ("subagent work missing", summary.subagents_missing, "trace(s) (heuristic: by tool name)"),
        ("duplicate call ids", summary.duplicate_call_ids, "trace(s)"),
        ("redacted values", summary.redacted, "trace(s)"),
        ("empty traces", summary.empty, ""),
        ("unreadable files", len(summary.unreadable), ""),
    ]
    for label, n, unit in counts:
        if n:
            lines.append(f"  {label:<28}{n} {unit}".rstrip())
    fixes = [name for name in MEASURES if cov[name] is not None and cov[name] < 90]
    if fixes:
        lines.append("  To make these traces checkable:")
        lines.extend(f"    - {_HINTS[name]}" for name in fixes)
    if groups and len(groups) > 1:
        lines.append("")
        lines.append(
            f"  {'group':<34}{'traces':>7}{'results':>9}{'outcome':>9}{'args':>7}"
            f"{'reason':>8}  issues"
        )
        for name, g in sorted(groups.items()):
            c = g.coverage()
            cells = "".join(
                f"{('-' if c[k] is None else f'{c[k]:.0f}%'):>{w}}"
                for k, w in (("results", 9), ("outcome", 9), ("arguments", 7), ("reasoning", 8))
            )
            flags = [
                label
                for label, n in (
                    ("unanswered", g.ends_unanswered),
                    ("subagents", g.subagents_missing),
                    ("dup-ids", g.duplicate_call_ids),
                    ("redacted", g.redacted),
                    ("empty", g.empty),
                )
                if n
            ]
            lines.append(f"  {name[:34]:<34}{g.traces:>7}{cells}  {', '.join(flags)}")
    return "\n".join(lines)


def _text(step: Any) -> str:
    value = step.args if isinstance(step, ToolCall) else getattr(step, "content", None)
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)
