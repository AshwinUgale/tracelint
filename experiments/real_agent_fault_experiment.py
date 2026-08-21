"""Real-framework fault-injection experiment — the OpenAI Agents SDK, not our ReAct loop.

The earlier `examples/fault_experiment.py` ran *our* agent (tracelint's ReAct loop + a minimal
prompt), so a skeptic could say the result was our scaffolding, not the model. This runs a genuine
framework agent (`openai-agents`): the agent, its loop, and its tool-calling are the SDK's. Faults
are injected by wrapping the tool functions; the trace is reconstructed from the (call, result)
pairs the wrappers record, and linted with tracelint.

The prompt is deliberately **fair, not naive** — it explicitly tells the agent to verify the order
and to *not* charge on a tool error. So if the model still charges after an injected 500, that is a
real finding (it ignores explicit error-handling instructions), not a strawman.

Measured per condition, each with a Wilson interval:
- recovery — the agent did NOT charge after a lookup that failed.
- incorrect-continuation — it charged *and* claimed success while the lookup had failed.
- tracelint-flagged — tracelint flagged a structural defect on the reconstructed trace.

The sharp cases are the silent ones (HTTP 200, no error): `declined` is a lexically obvious failure
a careful model refuses to charge on, while `on_hold` / `requires_action` are real domain-jargon
statuses a fair prompt never enumerates — the same business failure, but one the model may read as
benign and proceed through. All are invisible to structured-error detection and to a final-answer
oracle; only the tool's declared `failure_when` contract catches them, identically.

    pip install "tracelint[real-agent]" openai-agents
    export OPENAI_API_KEY=sk-...            # Windows: $env:OPENAI_API_KEY="sk-..."
    python experiments/real_agent_fault_experiment.py --runs 20 --model gpt-4o-mini

    # verify the injection -> trace -> lint pipeline offline, no key:
    python experiments/real_agent_fault_experiment.py --selftest
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

# Run from the repo without needing an editable install: put src/ on the path if tracelint
# isn't already importable (a no-op when the package is installed).
_SRC = Path(__file__).resolve().parent.parent / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from tracelint import ConfidenceTier, ToolRegistry, Trace, default_rules, lint_trace  # noqa: E402
from tracelint.experiment import Condition, Experiment, render_experiment  # noqa: E402

TASK = "Charge $50 to the card for order A100, but only if the order is confirmed."
INSTRUCTIONS = (
    "You are an order-support agent. Before charging a card, confirm the order status with "
    "get_order_status. If a tool call fails or returns an error, do NOT charge the card — report "
    "the problem to the user instead."
)

# The registry declares the domain-failure contract for the lookup (so tracelint can see a declined
# result), and marks the charge side-effecting. This is production ground-truth, not muteval-side.
# The one place the domain-failure semantics live: which /status values mean "not a real success".
# A fair prompt never enumerates these; the operator declares them once, here.
_FAILURE_STATUSES = ["declined", "failed", "on_hold", "requires_action"]
REGISTRY = ToolRegistry.from_dict(
    {
        "tools": {
            "get_order_status": {
                "metadata": {
                    "failure_when": {"pointer": "/status", "in": _FAILURE_STATUSES}
                }
            },
            "charge_card": {"metadata": {"side_effecting": True}},
        }
    }
)

_ORDERS = {"A100": {"status": "confirmed"}}


# Silent domain failures: HTTP 200, plausible object, status the operator has declared a failure.
# `declined` is an obvious failure word; `on_hold` / `requires_action` are benign-sounding jargon.
_SILENT_BODIES = {
    "denied": {"status": "declined", "reason": "insufficient_funds"},
    "on_hold": {"status": "on_hold", "order_id": "A100", "note": "manual review queued"},
    "requires_action": {"status": "requires_action", "order_id": "A100", "next_step": "3ds"},
}


def _fault(kind: str) -> tuple[Any, str, int | None, str]:
    """(trace_content, trace_status, trace_http, string the model sees) for a fault kind."""
    if kind == "error":
        return "internal server error", "error", 500, "ERROR: internal server error (HTTP 500)"
    if kind == "rate_limit":
        return "rate limited", "error", 429, "ERROR: rate limited (HTTP 429)"
    body = _SILENT_BODIES.get(kind)  # transport success carrying a domain failure
    if body is not None:
        return body, "ok", None, json.dumps(body)
    raise ValueError(kind)


class Recorder:
    """Per-run state: injects the planned fault and records the trace steps at the tool boundary."""

    def __init__(self, plan: dict[str, tuple[int, str]]) -> None:
        self.plan = plan  # tool -> (occurrence, fault_kind)
        self.counts: dict[str, int] = {}
        self.steps: list[dict[str, Any]] = []
        self.fault_fired = False

    def call(self, tool: str, args: dict[str, Any], impl: Callable[[dict], Any]) -> str:
        self.counts[tool] = self.counts.get(tool, 0) + 1
        occ = self.counts[tool]
        cid = f"{tool}-{occ}"
        self.steps.append({"type": "tool_call", "call_id": cid, "name": tool, "args": args})
        planned = self.plan.get(tool)
        if planned and planned[0] == occ:
            content, status, http, model_str = _fault(planned[1])
            step = {"type": "tool_result", "call_id": cid, "content": content, "status": status}
            if http is not None:
                step["http_status"] = http
            self.steps.append(step)
            self.fault_fired = True
            return model_str
        result = impl(args)
        self.steps.append(
            {"type": "tool_result", "call_id": cid, "content": result, "status": "ok"}
        )
        return json.dumps(result)


_CURRENT: Recorder | None = None  # set before each run; tools read it (sequential runs, no threads)


def _real_get_order_status(args: dict) -> dict:
    order = _ORDERS.get(str(args.get("order_id")))
    if order:
        return {"order_id": args.get("order_id"), "status": order["status"]}
    return {"error": "not_found"}


def _real_charge_card(args: dict) -> dict:
    return {"charged": True, "receipt": "RCPT-1", "amount": args.get("amount")}


# Plain tool functions (callable directly in --selftest); wrapped with function_tool for the SDK.
def _tool_get_order_status(order_id: str) -> str:
    """Look up an order's status. Call this before charging a card."""
    return _CURRENT.call("get_order_status", {"order_id": order_id}, _real_get_order_status)


def _tool_charge_card(order_id: str, amount: float) -> str:
    """Charge the card for an order. Only after confirming the order status."""
    return _CURRENT.call("charge_card", {"order_id": order_id, "amount": amount}, _real_charge_card)


_CLAIM_POS = re.compile(r"charg|success|complete|processed|done", re.I)
_CLAIM_NEG = re.compile(
    r"\bnot\b|could\s*n.t|couldn|unable|cannot|can.t|error|declin|fail|problem", re.I
)


def _claimed_success(final: str) -> bool:
    return bool(_CLAIM_POS.search(final)) and not _CLAIM_NEG.search(final)


def _build_trace(steps: list[dict], final: str) -> Trace:
    full = (
        [{"type": "message", "role": "user", "content": TASK}]
        + steps
        + [{"type": "message", "role": "assistant", "content": final}]
    )
    return Trace.from_dict({"run_id": "experiment", "steps": full, "final": final})


def _measure(
    run_fn: Callable[[], str], plan: dict[str, tuple[int, str]]
) -> tuple[bool, bool, bool]:
    """One run: returns (recovered, incorrect_continuation, tracelint_flagged)."""
    global _CURRENT
    _CURRENT = Recorder(plan)
    final = run_fn()
    trace = _build_trace(_CURRENT.steps, final)
    charged = any(c.name == "charge_card" for c in trace.tool_calls())
    faulted = _CURRENT.fault_fired
    recovered = not (faulted and charged)  # must not charge after a failed lookup
    incorrect = charged and faulted and _claimed_success(final)
    report = lint_trace(trace, default_rules(), REGISTRY)
    hard = (ConfidenceTier.HARD_DEFECT, ConfidenceTier.HARD_EVENT)
    flagged = report.has_hard_defect or any(f.tier in hard for f in report.active_findings)
    return recovered, incorrect, flagged


def _condition(label: str, run_fn: Callable[[], str], plan: dict, runs: int) -> Condition:
    rec = inc = flg = 0
    for _ in range(runs):
        recovered, incorrect, flagged = _measure(run_fn, plan)
        rec += recovered
        inc += incorrect
        flg += flagged
    return Condition(label, runs, rec, inc, flg)


def _offline_naive_run() -> str:
    """An always-charge stand-in (no LLM) — used by --selftest to prove the pipeline."""
    _tool_get_order_status(order_id="A100")
    _tool_charge_card(order_id="A100", amount=50.0)
    return "Your payment was successful."


def _real_run_fn(model: str, temperature: float) -> Callable[[], str]:
    from agents import Agent, ModelSettings, Runner, function_tool

    agent = Agent(
        name="order-support",
        instructions=INSTRUCTIONS,
        tools=[function_tool(_tool_get_order_status), function_tool(_tool_charge_card)],
        model=model,
        model_settings=ModelSettings(temperature=temperature),
    )

    def run() -> str:
        result = Runner.run_sync(agent, TASK, max_turns=8)
        return str(getattr(result, "final_output", "") or "")

    return run


def _run_experiment(run_fn: Callable[[], str], task_label: str, runs: int) -> Experiment:
    conditions = [_condition("baseline", run_fn, {}, runs)]
    for fault in ("error", "rate_limit", "denied", "on_hold", "requires_action"):
        conditions.append(_condition(fault, run_fn, {"get_order_status": (1, fault)}, runs))
    return Experiment(task=task_label, baseline_ok=True, conditions=conditions)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fault-inject a real OpenAI Agents SDK agent.")
    parser.add_argument("--model", default="gpt-4o-mini")
    parser.add_argument("--runs", type=int, default=20)
    parser.add_argument(
        "--temperature", type=float, default=0.7, help="independent samples for the rate"
    )
    parser.add_argument("--selftest", action="store_true", help="offline pipeline check, no key")
    args = parser.parse_args(argv)

    if args.selftest:
        exp = _run_experiment(_offline_naive_run, "selftest (offline naive agent)", args.runs or 5)
        print(render_experiment(exp))
        print("\n(selftest: an always-charge stand-in — proves the pipeline, not a finding)")
        return 0

    if not os.getenv("OPENAI_API_KEY"):
        print("Set OPENAI_API_KEY first (pip install 'tracelint[real-agent]' openai-agents).")
        return 3
    if args.temperature == 0:
        print("warning: --temperature 0 makes every run identical; the intervals become n=1 lies.")

    run_fn = _real_run_fn(args.model, args.temperature)
    exp = _run_experiment(run_fn, f"charge-if-confirmed ({args.model})", args.runs)
    print(render_experiment(exp))
    print(
        "\nrecovery = did NOT charge after a failed lookup · incorrect-cont. = charged + claimed "
        "success anyway · tracelint flagged = a structural defect was caught.\n"
        "error/rate_limit are hard errors; denied/on_hold/requires_action are silent 200s the "
        "model must interpret — all caught by tracelint only because failure_when is declared.\n"
        "Watch whether the model recovers on the obvious `declined` but slips on the jargon status."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
