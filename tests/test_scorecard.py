"""Phase 6 — the recovery scorecard (spec §II.7; learning-doc 03 §2, §4)."""

from __future__ import annotations

from tracelint import FaultType, run_scorecard
from tracelint.agent import build_demo_toolset, build_recovery_task
from tracelint.agent.react import ReActAgent
from tracelint.agent.scripted import PolicyLLM, final, tool
from tracelint.scorecard import RunContext, Task, all_of, final_answer_not_claims, tool_called

ERR_FAULTS = [FaultType.TIMEOUT, FaultType.ERROR, FaultType.RATE_LIMIT]


def test_robust_agent_recovers_from_errors():
    sc = run_scorecard(build_recovery_task(buggy=False), ERR_FAULTS)
    assert sc.baseline_ok is True
    assert sc.mode == "correctness"
    assert all(r.rate == 1.0 for r in sc.results)  # robust agent never cancels on a failed lookup


def test_buggy_agent_fails_to_recover():
    sc = run_scorecard(build_recovery_task(buggy=True), ERR_FAULTS)
    assert sc.baseline_ok is True
    # It cancels despite the errored lookup → violates the safety oracle every time.
    assert all(r.rate == 0.0 for r in sc.results)


def test_recovery_rates_carry_a_confidence_interval():
    sc = run_scorecard(build_recovery_task(buggy=False), [FaultType.ERROR], runs=5)
    r = sc.results[0]
    assert r.total == 5
    assert 0.0 <= r.ci[0] <= r.ci[1] <= 1.0


def test_baseline_that_fails_the_oracle_blocks_measurement():
    # An oracle that can never be satisfied → baseline invalid → no recovery numbers.
    task = Task(
        name="broken",
        build_toolset=build_demo_toolset,
        build_agent=lambda ts: ReActAgent(PolicyLLM(lambda steps: final("done")), ts),
        task_text="do nothing",
        oracle=tool_called("a_tool_never_called"),
    )
    sc = run_scorecard(task, [FaultType.ERROR])
    assert sc.baseline_ok is False
    assert sc.results == []
    assert "task invalid" in sc.note


def test_behavioral_mode_when_no_oracle_is_labeled_weaker():
    # No oracle → behavioral recovery: the buggy agent still "completes", so it looks recovered,
    # which is exactly the weaker claim the mode is labeled as.
    task = Task(
        name="cancel-buggy-no-oracle",
        build_toolset=build_demo_toolset,
        build_agent=lambda ts: ReActAgent(
            PolicyLLM(
                lambda steps: (
                    tool("get_order_status", {"order_id": "4521"})
                    if not [s for s in steps if getattr(s, "name", None) == "get_order_status"]
                    else final("Order cancelled.")
                )
            ),
            ts,
        ),
        task_text="cancel 4521",
        oracle=None,
    )
    sc = run_scorecard(task, [FaultType.ERROR])
    assert sc.mode == "behavioral"
    assert sc.results[0].rate == 1.0  # "didn't crash" — overstates resilience
    assert "weaker than correctness" in sc.note


def test_oracle_builders():
    toolset = build_demo_toolset()
    trace_ctx = RunContext(
        trace=ReActAgent(
            PolicyLLM(
                lambda steps: (
                    tool("cancel_order", {"order_id": "4521", "reason": "not_shipped"})
                    if not [s for s in steps if getattr(s, "name", None) == "cancel_order"]
                    else final("All set — nothing pending.")
                )
            ),
            toolset,
        ).run("t"),
        toolset=toolset,
    )
    assert tool_called("cancel_order")(trace_ctx) is True
    assert tool_called("cancel_order", {"order_id": "4521"})(trace_ctx) is True
    assert tool_called("refund")(trace_ctx) is False
    assert final_answer_not_claims("refund")(trace_ctx) is True
    assert all_of(tool_called("cancel_order"), final_answer_not_claims("refund"))(trace_ctx) is True
