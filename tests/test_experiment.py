"""The fault-injection experiment harness (deterministic, offline via the scripted agents).

These encode the demonstration the harness exists to produce: a structured error is caught, and the
silent 200-with-`declined` failure is invisible to everything until `failure_when` is declared.
"""

from __future__ import annotations

from tracelint.agent import build_recovery_task
from tracelint.experiment import render_experiment, run_experiment
from tracelint.injection import FaultType
from tracelint.scorecard import final_answer_contains
from tracelint.tools import ToolRegistry

CLAIM = final_answer_contains("has been cancelled")  # the buggy agent's success claim


def _cond(exp, label):
    return next(c for c in exp.conditions if c.label == label)


def test_buggy_agent_fails_claims_success_and_tracelint_catches_the_error():
    exp = run_experiment(
        build_recovery_task(buggy=True),
        [FaultType.ERROR],
        runs=6,
        target="get_order_status",
        success_claim=CLAIM,
    )
    err = _cond(exp, "error")
    assert err.recovery_rate == 0.0  # never recovers
    assert err.incorrect_rate == 1.0  # claims success anyway
    assert err.flagged_rate == 1.0  # tracelint catches every one (R2a)


def test_robust_agent_recovers_and_does_not_falsely_claim_success():
    exp = run_experiment(
        build_recovery_task(buggy=False),
        [FaultType.ERROR],
        runs=6,
        target="get_order_status",
        success_claim=CLAIM,
    )
    err = _cond(exp, "error")
    assert err.recovery_rate == 1.0 and err.incorrect_rate == 0.0


def test_denied_is_invisible_by_default_and_caught_only_with_failure_when():
    task = build_recovery_task(buggy=True)
    default = run_experiment(
        task, [FaultType.DENIED], runs=6, target="get_order_status", success_claim=CLAIM
    )
    # A 200-with-declined body: the oracle can't see it (it "recovers") and neither can tracelint.
    assert _cond(default, "denied").flagged_rate == 0.0

    registry = ToolRegistry.from_dict(
        {
            "tools": {
                "get_order_status": {
                    "metadata": {"failure_when": {"pointer": "/status", "in": ["declined"]}}
                }
            }
        }
    )
    with_fw = run_experiment(
        task,
        [FaultType.DENIED],
        runs=6,
        target="get_order_status",
        success_claim=CLAIM,
        registry=registry,
    )
    # Declaring the domain-failure contract makes tracelint the sole detector.
    assert _cond(with_fw, "denied").flagged_rate == 1.0


def test_baseline_that_never_passes_is_not_measured():
    task = build_recovery_task(buggy=True)
    task.oracle = lambda ctx: False  # a task whose baseline can never satisfy the oracle
    exp = run_experiment(task, [FaultType.ERROR], runs=3, target="get_order_status")
    assert not exp.baseline_ok and exp.conditions == []


def test_render_is_a_table_with_intervals():
    exp = run_experiment(
        build_recovery_task(buggy=True), [FaultType.ERROR], runs=4, target="get_order_status"
    )
    out = render_experiment(exp)
    assert "recovery" in out and "tracelint flagged" in out and "[" in out  # rate [lo,hi]
