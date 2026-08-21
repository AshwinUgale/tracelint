"""The fault-injection example's wiring (oracle, claim, failure_when registry), tested offline.

The real path needs an API key; here a scripted LLM drives the same Task so the example's
before/after on the `denied` fault is exercised deterministically.
"""

from __future__ import annotations

from examples.fault_experiment import (
    _CLAIMED_CANCEL,
    _REGISTRY_WITH_FAILURE_WHEN,
    build_real_task,
)
from tracelint.agent.scripted import ScriptedLLM, final, tool
from tracelint.experiment import run_experiment
from tracelint.injection import FaultType


def _buggy_llm():
    # Always cancels regardless of the lookup result — the error-ignoring agent.
    return ScriptedLLM(
        [
            tool("get_order_status", {"order_id": "4521"}),
            tool("cancel_order", {"order_id": "4521", "reason": "not_shipped"}),
            final("Order 4521 has been cancelled."),
        ]
    )


def _cond(exp, label):
    return next(c for c in exp.conditions if c.label == label)


def test_example_denied_before_and_after_failure_when():
    task = build_real_task(build_llm=_buggy_llm)
    common = dict(runs=4, target="get_order_status", success_claim=_CLAIMED_CANCEL)

    default = run_experiment(task, [FaultType.ERROR, FaultType.DENIED], **common)
    assert _cond(default, "error").flagged_rate == 1.0  # structured error caught
    assert _cond(default, "denied").flagged_rate == 0.0  # silent 200-declined invisible

    with_fw = run_experiment(
        task, [FaultType.DENIED], registry=_REGISTRY_WITH_FAILURE_WHEN, **common
    )
    assert _cond(with_fw, "denied").flagged_rate == 1.0  # declared contract catches it
