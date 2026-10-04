"""A CI baseline: accept the findings a project already has, fail only on new ones.

A team adopting tracelint on traces that already have findings needs the build to stay green while
it works through them, and to go red the moment a run gets worse. ``tracelint check --baseline
tracelint-baseline.json --update-baseline`` records what the traces show today; later runs with
``--baseline`` accept up to that many of each finding and fail on anything beyond it.

**Matching survives a re-run of the agent.** A finding is matched by its trace file and its
:class:`~tracelint.identity.FindingKey` — the rule, the tools and fields involved, the signal —
never by step position, value or run id, which change every time an agent runs. The baseline
counts each key: two accepted R2b findings on ``run_release_pipeline -> deploy`` accept two, and a
third fails. A recorded finding also accepts the same key at a lower tier, never a higher one: an
accepted candidate that becomes a hard defect is new.

Only findings at or above the gate (``fail_on``, a hard defect by default) are recorded and
checked; below it nothing fails anyway. Config ignores apply first. A baselined finding stays in
the report as ignored, with the reason "in the baseline".

**The coverage ratchet.** A run can also get worse by checking less: content capture switched off
leaves every argument unknown, and a deleted ``tools.json`` leaves R1 nothing to validate — and the
report goes green while seeing nothing. So the baseline also records, per trace file, which tools
each rule could not check and which rules checked something. A run fails the gate (exit 1) when a
rule can no longer check a tool it used to, or a rule that checked something now checks nothing
(``0 / N``). Like findings, these are kinds, not counts. ``ratchet = false`` turns it off.

Paths are stored relative to the baseline file, so it works from any working directory, and
updating from a subset of traces keeps the entries for the others.
"""

from __future__ import annotations

import json
import os
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tracelint.findings import ConfidenceTier, Finding, LintReport
from tracelint.identity import FindingKey, finding_key

BASELINE_VERSION = 1
ACCEPTED = "in the baseline"


class BaselineError(ValueError):
    """The baseline file is missing or invalid; ``tracelint check`` exits 3 with this message."""


@dataclass(frozen=True)
class Accepted:
    """A kind of finding the baseline accepts, and how many."""

    key: FindingKey
    tier: ConfidenceTier
    count: int

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "rule": self.key.rule,
            "kind": self.key.finding_type,
            "tier": self.tier.value,
            "count": self.count,
        }
        if self.key.tools:
            out["tools"] = list(self.key.tools)
        if self.key.fields:
            out["fields"] = list(self.key.fields)
        if self.key.signal:
            out["signal"] = self.key.signal
        return out

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Accepted:
        key = FindingKey(
            rule=str(data["rule"]),
            finding_type=str(data["kind"]),
            tools=tuple(data.get("tools", ())),
            fields=tuple(data.get("fields", ())),
            signal=data.get("signal"),
        )
        return cls(key, ConfidenceTier(data["tier"]), int(data["count"]))


@dataclass
class FileBaseline:
    """What one trace file showed when the baseline was recorded."""

    accepted: list[Accepted] = field(default_factory=list)
    unchecked: set[tuple[str, str]] = field(default_factory=set)  # (rule, tool) it couldn't check
    checked: set[str] = field(default_factory=set)  # rules that evaluated at least one unit

    def to_dict(self) -> dict[str, Any]:
        order = sorted(self.accepted, key=lambda a: (a.key.rule, a.key.finding_type, repr(a)))
        return {
            "findings": [a.to_dict() for a in order],
            "unchecked": [{"rule": r, "tool": t} for r, t in sorted(self.unchecked)],
            "checked": sorted(self.checked),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FileBaseline:
        return cls(
            accepted=[Accepted.from_dict(a) for a in data.get("findings", [])],
            unchecked={(str(u["rule"]), str(u["tool"])) for u in data.get("unchecked", [])},
            checked={str(r) for r in data.get("checked", [])},
        )


@dataclass
class Baseline:
    """Per trace file (relative to the baseline file), what it accepts."""

    path: Path
    files: dict[str, FileBaseline] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> Baseline:
        if not path.is_file():
            raise BaselineError(
                f"baseline {path} not found; record one with `tracelint check ... --baseline "
                f"{path} --update-baseline`"
            )
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("version") != BASELINE_VERSION:
                raise BaselineError(f"{path}: unsupported baseline version {data.get('version')!r}")
            files = {name: FileBaseline.from_dict(f) for name, f in data["files"].items()}
        except BaselineError:
            raise
        except (json.JSONDecodeError, KeyError, TypeError, ValueError, AttributeError) as exc:
            raise BaselineError(f"{path}: not a tracelint baseline ({exc})") from exc
        return cls(path, files)

    def save(self) -> None:
        data = {
            "version": BASELINE_VERSION,
            "files": {name: self.files[name].to_dict() for name in sorted(self.files)},
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
        self.path.write_bytes(text.encode("utf-8"))

    def name_for(self, trace_path: str) -> str:
        """``trace_path`` as the baseline stores it: relative to the baseline file, with ``/``."""
        absolute = os.path.abspath(trace_path)
        try:
            relative = os.path.relpath(absolute, os.path.abspath(self.path.parent))
        except ValueError:  # another drive on Windows
            relative = absolute
        return relative.replace(os.sep, "/")


def gated(report: LintReport, fail_on: ConfidenceTier) -> list[Finding]:
    """The active findings of ``report`` that reach the gate."""
    return [f for f in report.active_findings if f.tier.rank >= fail_on.rank]


def record(reports: Sequence[LintReport], fail_on: ConfidenceTier) -> FileBaseline:
    """What one trace file's runs show now, as a baseline would keep it."""
    counts: Counter[tuple[FindingKey, ConfidenceTier]] = Counter()
    for report in reports:
        for finding in gated(report, fail_on):
            counts[(finding_key(finding), finding.tier)] += 1
    accepted = [Accepted(key, tier, n) for (key, tier), n in counts.items()]
    return FileBaseline(accepted, _unchecked(reports), _checked(reports))


def apply(
    entry: FileBaseline, reports: Sequence[LintReport], fail_on: ConfidenceTier, *, ratchet: bool
) -> int:
    """Mark the findings ``entry`` accepts, and record coverage regressions on the file's first
    report. Return how many accepted findings no longer occur (stale)."""
    available: dict[FindingKey, list[ConfidenceTier]] = {}
    for accepted in entry.accepted:
        available.setdefault(accepted.key, []).extend([accepted.tier] * accepted.count)
    findings = [f for report in reports for f in gated(report, fail_on)]
    for finding in sorted(findings, key=lambda f: -f.tier.rank):  # the most severe claim first
        tiers = available.get(finding_key(finding), [])
        fitting = [t for t in tiers if t.rank >= finding.tier.rank]
        if fitting:
            tiers.remove(min(fitting, key=lambda t: t.rank))  # the closest fit
            finding.ignored_reason = ACCEPTED
    if ratchet and reports:
        reports[0].gate_failures.extend(_regressions(entry, reports))
    return sum(len(tiers) for tiers in available.values())


def _unchecked(reports: Iterable[LintReport]) -> set[tuple[str, str]]:
    """(rule, tool) for each tool a rule could not check, from the suppressions that name one."""
    spots: set[tuple[str, str]] = set()
    for report in reports:
        for suppression in report.suppressions:
            evidence = suppression.evidence
            tools = [evidence["tool"]] if isinstance(evidence.get("tool"), str) else []
            listed = evidence.get("tools")
            if isinstance(listed, list):
                tools += [t for t in listed if isinstance(t, str)]
            spots.update((suppression.rule, tool) for tool in tools)
    return spots


def _coverage(reports: Iterable[LintReport]) -> dict[str, tuple[int, int, str]]:
    """Per rule, (evaluatable, total, unit) summed over the file's runs."""
    totals: dict[str, tuple[int, int, str]] = {}
    for report in reports:
        for c in report.coverage:
            done, total, _ = totals.get(c.rule, (0, 0, c.unit))
            totals[c.rule] = (done + c.evaluatable, total + c.total, c.unit)
    return totals


def _checked(reports: Sequence[LintReport]) -> set[str]:
    return {rule for rule, (done, _, _) in _coverage(reports).items() if done > 0}


def _regressions(entry: FileBaseline, reports: Sequence[LintReport]) -> list[str]:
    out = []
    for rule, tool in sorted(_unchecked(reports) - entry.unchecked):
        out.append(f"{rule} can no longer check {tool!r} (it could in the baseline)")
    for rule, (done, total, unit) in sorted(_coverage(reports).items()):
        if rule in entry.checked and total > 0 and done == 0:
            out.append(f"{rule} checked {done}/{total} {unit} (it checked some in the baseline)")
    return out
