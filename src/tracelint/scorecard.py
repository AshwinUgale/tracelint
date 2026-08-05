"""Recovery scorecard (spec §II.7; learning-doc 03 §2, §4).

"Recovered from a timeout 80% of the time" is meaningless without knowing what a *recovered* run
looks like — a **success oracle** per task (spec §II.7). So the scorecard is built on the
baseline-vs-variant method (03 §2):

1. Run the task with no fault → **baseline**. If the baseline does not satisfy the oracle, the task
   itself is broken and recovery cannot be measured (fail closed — no numbers).
2. Run the same task with a fault injected at a point → **variant**, ``runs`` times per fault.
3. Recovery rate per fault = fraction of variants that still satisfy the oracle, with a Wilson CI.

Two grades of recovery (03 §4), never conflated:

- **correctness recovery** — the variant still satisfies the oracle (the real claim). Needs an
  oracle.
- **behavioral recovery** — the run merely *completed* (produced a final answer) without an
  unhandled error. Reported only when no oracle is supplied, and labeled as the weaker claim — a
  run can behaviorally recover while returning a confident wrong answer.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from tracelint.agent.react import ReActAgent
from tracelint.agent.tools import AgentToolset
from tracelint.injection import FaultInjector, FaultType, TargetedInjection
from tracelint.stats import wilson_interval
from tracelint.trace import Trace
from tracelint.valueutil import normalize


@dataclass
class RunContext:
    """One completed run: the recorded trace plus the underlying toolset (for end-state checks)."""

    trace: Trace
    toolset: AgentToolset


Oracle = Callable[[RunContext], bool]


@dataclass
class Task:
    """A task with everything needed to run it and judge success (spec §II.7)."""

    name: str
    build_toolset: Callable[[], AgentToolset]
    build_agent: Callable[[object], ReActAgent]  # given a toolset/injector → an agent
    task_text: str
    oracle: Oracle | None = None


# --- Oracle builders (deterministic; no judge) -----------------------------------------


def tool_called(name: str, args: dict | None = None) -> Oracle:
    """The run called ``name`` (optionally with arguments matching ``args``)."""

    def check(ctx: RunContext) -> bool:
        for call in ctx.trace.tool_calls():
            if call.name != name:
                continue
            if args is None or all(
                normalize(call.args.get(k)) == normalize(v) for k, v in args.items()
            ):
                return True
        return False

    return check


def final_answer_contains(text: str) -> Oracle:
    def check(ctx: RunContext) -> bool:
        return ctx.trace.final is not None and text.lower() in str(ctx.trace.final).lower()

    return check


def final_answer_not_claims(text: str) -> Oracle:
    def check(ctx: RunContext) -> bool:
        return not (ctx.trace.final is not None and text.lower() in str(ctx.trace.final).lower())

    return check


def state_check(fn: Callable[[AgentToolset], bool]) -> Oracle:
    """An expected-end-state oracle over the underlying toolset after the run."""
    return lambda ctx: bool(fn(ctx.toolset))


def all_of(*oracles: Oracle) -> Oracle:
    return lambda ctx: all(o(ctx) for o in oracles)


# --- Results ---------------------------------------------------------------------------


@dataclass
class FaultRecovery:
    fault: FaultType
    mode: str  # "correctness" | "behavioral"
    recovered: int
    total: int
    rate: float
    ci: tuple[float, float]


@dataclass
class Scorecard:
    task: str
    baseline_ok: bool
    has_oracle: bool
    mode: str
    results: list[FaultRecovery] = field(default_factory=list)
    note: str = ""


def _behavioral_ok(ctx: RunContext) -> bool:
    """Behavioral recovery: the agent produced a final answer (completed its turn)."""
    return ctx.trace.final is not None


def _one_run(task: Task, plan: TargetedInjection | None = None, seed: int = 0) -> RunContext:
    toolset = task.build_toolset()
    interface = FaultInjector(toolset, plan, seed=seed) if plan is not None else toolset
    agent = task.build_agent(interface)
    trace = agent.run(task.task_text, run_id="scorecard")
    return RunContext(trace=trace, toolset=toolset)


def _evaluate(task: Task, ctx: RunContext) -> bool:
    return bool(task.oracle(ctx)) if task.oracle else _behavioral_ok(ctx)


def run_scorecard(
    task: Task,
    faults: list[FaultType],
    *,
    targets: list[str | None] | None = None,
    runs: int = 1,
    seed: int = 0,
) -> Scorecard:
    """Measure per-fault recovery for ``task`` via the baseline-vs-variant method."""
    mode = "correctness" if task.oracle else "behavioral"
    baseline = _one_run(task)
    if not _evaluate(task, baseline):
        return Scorecard(
            task=task.name,
            baseline_ok=False,
            has_oracle=task.oracle is not None,
            mode=mode,
            note="baseline does not satisfy the oracle — task invalid, recovery not measured",
        )

    inject_targets = targets if targets is not None else [None]
    results: list[FaultRecovery] = []
    for fault in faults:
        recovered = total = 0
        for target in inject_targets:
            for i in range(runs):
                ctx = _one_run(task, TargetedInjection(fault, tool=target), seed=seed + i)
                total += 1
                recovered += 1 if _evaluate(task, ctx) else 0
        results.append(
            FaultRecovery(
                fault=fault,
                mode=mode,
                recovered=recovered,
                total=total,
                rate=recovered / total if total else 0.0,
                ci=wilson_interval(recovered, total),
            )
        )

    note = "" if task.oracle else "behavioral recovery only (no oracle) — weaker than correctness"
    return Scorecard(task.name, True, task.oracle is not None, mode, results, note)


def render_scorecard(sc: Scorecard) -> str:
    lines = [f"recovery scorecard: {sc.task}"]
    if not sc.baseline_ok:
        lines.append(f"  {sc.note}")
        return "\n".join(lines)
    header = f"  mode: {sc.mode} recovery"
    if sc.mode == "behavioral":
        header += "  (= 'completed without crashing'; weaker than correctness — no oracle supplied)"
    lines.append(header)
    for r in sc.results:
        lo, hi = r.ci
        lines.append(
            f"    {r.fault.value:<14} {r.recovered}/{r.total}  "
            f"rate={r.rate:.2f}  95% CI [{lo:.2f}, {hi:.2f}]"
        )
    return "\n".join(lines)
