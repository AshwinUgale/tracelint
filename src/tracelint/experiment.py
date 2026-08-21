"""Fault-injection experiment: measure how an agent behaves under injected faults.

The recovery scorecard answers one question — "did the run still satisfy the oracle?" This runs the
same baseline-vs-variant method but records the three numbers an agent-reliability study actually
needs, each with a Wilson interval:

- **recovery rate** — the oracle still passes despite the fault (correctness recovery).
- **incorrect-continuation rate** — the agent *claimed success* while the oracle failed: the
  dangerous silent-failure case where it never realized anything went wrong.
- **tracelint-caught rate** — of the runs that actually failed, the fraction where tracelint flagged
  a structural defect (an R2 finding / a hard_defect). This ties the linter to the ground truth: on
  the runs the oracle says were broken, did the deterministic checker catch it?

The point is a *result you did not author*: a real agent, real tools, faults injected at the tool
boundary, judged by an independent oracle — not a scripted outcome. The scripted demo task is
deterministic (so the harness is testable offline); point a :class:`~tracelint.scorecard.Task`'s
``build_agent`` at a real LLM to get real evidence. Injecting the ``DENIED`` fault (HTTP 200 with a
``status: declined`` body) on a side-effecting tool is the sharpest case — invisible to everything
unless the tool declares a ``failure_when`` predicate.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from tracelint.injection import FaultInjector, FaultType, TargetedInjection
from tracelint.rules import default_rules, lint_trace
from tracelint.rules.base import Rule
from tracelint.scorecard import Oracle, RunContext, Task
from tracelint.stats import wilson_interval
from tracelint.tools import ToolRegistry


@dataclass
class Condition:
    """Aggregated outcomes for one condition (baseline, or one injected fault)."""

    label: str
    n: int
    recovered: int
    incorrect_continuation: int
    flagged: int  # tracelint flagged a structural defect (independent of the oracle)

    @property
    def failed(self) -> int:
        return self.n - self.recovered

    @property
    def recovery_rate(self) -> float:
        return self.recovered / self.n if self.n else 0.0

    @property
    def recovery_ci(self) -> tuple[float, float]:
        return wilson_interval(self.recovered, self.n)

    @property
    def incorrect_rate(self) -> float:
        return self.incorrect_continuation / self.n if self.n else 0.0

    @property
    def incorrect_ci(self) -> tuple[float, float]:
        return wilson_interval(self.incorrect_continuation, self.n)

    @property
    def flagged_rate(self) -> float:
        return self.flagged / self.n if self.n else 0.0

    @property
    def flagged_ci(self) -> tuple[float, float]:
        return wilson_interval(self.flagged, self.n)


@dataclass
class Experiment:
    task: str
    baseline_ok: bool
    conditions: list[Condition] = field(default_factory=list)
    note: str = ""


def _run(task: Task, plan: TargetedInjection | None, seed: int) -> RunContext:
    toolset = task.build_toolset()
    interface = FaultInjector(toolset, plan, seed=seed) if plan is not None else toolset
    agent = task.build_agent(interface)
    trace = agent.run(task.task_text, run_id="experiment")
    return RunContext(trace=trace, toolset=toolset)


def _tracelint_flagged(report) -> bool:
    """Did tracelint flag it — a hard defect, or a tool-error / mishandling (R2) finding?"""
    if report.has_hard_defect:
        return True
    return any(f.rule in ("R2a", "R2b") for f in report.active_findings)


def run_experiment(
    task: Task,
    faults: list[FaultType],
    *,
    runs: int = 20,
    seed: int = 0,
    target: str | None = None,
    success_claim: Oracle | None = None,
    rules: list[Rule] | None = None,
    registry: ToolRegistry | None = None,
) -> Experiment:
    """Run ``task`` at baseline and under each fault, ``runs`` times, and measure the three rates.

    ``target`` names the tool to inject on (``None`` = any). ``success_claim`` detects whether the
    agent's final answer claimed success (needed for the incorrect-continuation rate; defaults to
    "the agent produced any final answer", a weaker proxy). ``registry`` overrides the tool registry
    tracelint lints against — pass one declaring ``failure_when`` to show the before/after on the
    ``DENIED`` fault; defaults to the task toolset's own registry.
    """
    rules = rules or default_rules()

    def measure(plan: TargetedInjection | None, s: int) -> tuple[bool, bool, bool]:
        ctx = _run(task, plan, s)
        recovered = bool(task.oracle(ctx)) if task.oracle else (ctx.trace.final is not None)
        claimed = bool(success_claim(ctx)) if success_claim else (ctx.trace.final is not None)
        reg = registry if registry is not None else ctx.toolset.to_registry()
        report = lint_trace(ctx.trace, rules, reg)
        return recovered, claimed, _tracelint_flagged(report)

    baseline = [measure(None, seed + i) for i in range(runs)]
    if not any(r for r, _, _ in baseline):
        return Experiment(
            task.name, False, note="baseline never satisfies the oracle — task invalid"
        )

    conditions = [_aggregate("baseline", baseline)]
    for fault in faults:
        outcomes = [measure(TargetedInjection(fault, tool=target), seed + i) for i in range(runs)]
        conditions.append(_aggregate(fault.value, outcomes))
    return Experiment(task.name, True, conditions)


def _aggregate(label: str, outcomes: list[tuple[bool, bool, bool]]) -> Condition:
    n = len(outcomes)
    recovered = sum(1 for r, _, _ in outcomes if r)
    incorrect = sum(1 for r, c, _ in outcomes if c and not r)
    flagged = sum(1 for _, _, f in outcomes if f)
    return Condition(label, n, recovered, incorrect, flagged)


def render_experiment(exp: Experiment) -> str:
    """Render an experiment as a text table (rate with a 95% Wilson interval)."""
    lines = [f"fault-injection experiment: {exp.task}"]
    if not exp.baseline_ok:
        lines.append(f"  {exp.note}")
        return "\n".join(lines)
    lines.append(
        f"  {'condition':<14} {'recovery':>20} {'incorrect-cont.':>20} {'tracelint flagged':>20}"
    )

    def cell(rate: float, ci: tuple[float, float]) -> str:
        return f"{rate:.2f} [{ci[0]:.2f},{ci[1]:.2f}]"

    for c in exp.conditions:
        lines.append(
            f"  {c.label:<14} {cell(c.recovery_rate, c.recovery_ci):>20} "
            f"{cell(c.incorrect_rate, c.incorrect_ci):>20} "
            f"{cell(c.flagged_rate, c.flagged_ci):>20}"
        )
    return "\n".join(lines)
