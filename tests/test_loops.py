"""Phase 4 — R4 loop + R5 redundant call (spec §II.5, R4/R5; learning-doc 02 §4)."""

from __future__ import annotations

from tracelint import (
    ConfidenceTier,
    ToolMetadata,
    ToolRegistry,
    ToolSpec,
    build_trace,
    lint_trace,
)
from tracelint.rules import LoopRule, RedundantCallRule
from tracelint.trace import ResultStatus, ToolCall, ToolResult


def _r4(steps, registry=None):
    return lint_trace(build_trace("r", steps), [LoopRule()], registry or ToolRegistry())


def _r5(steps, registry=None):
    return lint_trace(build_trace("r", steps), [RedundantCallRule()], registry or ToolRegistry())


def _call_result(cid, name, args, content, status=ResultStatus.OK):
    return [ToolCall(cid, name, args), ToolResult(cid, content, status=status)]


# --- R4: loops -------------------------------------------------------------------------

def test_three_identical_noprogress_calls_is_a_loop():
    steps = []
    for i in range(3):
        steps += _call_result(f"c{i}", "search", {"q": "refunds"}, [], status=ResultStatus.OK)
    f = _r4(steps).active_findings[0]
    assert f.finding_type == "loop"
    assert f.tier is ConfidenceTier.CANDIDATE
    assert f.evidence["repeats"] == 3
    assert len(f.step_indices) == 3


def test_two_identical_calls_is_not_a_loop():
    steps = _call_result("c0", "search", {"q": "x"}, [], ResultStatus.OK)
    steps += _call_result("c1", "search", {"q": "x"}, [], ResultStatus.OK)
    steps.append(ToolCall("c2", "other", {}))  # 3 calls total, but the pair is below threshold
    steps.append(ToolResult("c2", {"ok": 1}, status=ResultStatus.OK))
    assert _r4(steps).active_findings == []


def test_progressing_poll_is_not_a_loop():
    # pending, pending, completed -> the advancing step breaks the identical run.
    steps = _call_result("c0", "poll", {"job": 7}, {"status": "pending"})
    steps += _call_result("c1", "poll", {"job": 7}, {"status": "pending"})
    steps += _call_result("c2", "poll", {"job": 7}, {"status": "completed"})
    assert _r4(steps).active_findings == []


def test_declared_polling_tool_is_trusted():
    # Five identical 'pending' polls, but the tool is declared polling -> not a loop.
    steps = []
    for i in range(5):
        steps += _call_result(f"c{i}", "poll", {"job": 7}, {"status": "pending"})
    registry = ToolRegistry({"poll": ToolSpec("poll", metadata=ToolMetadata(polling=True))})
    assert _r4(steps, registry).active_findings == []


def test_waiting_run_that_never_advances_is_flagged():
    # Not declared polling and never advances within the trace -> a candidate stuck loop.
    steps = []
    for i in range(3):
        steps += _call_result(f"c{i}", "poll", {"job": 7}, {"status": "pending"})
    f = _r4(steps).active_findings[0]
    assert f.tier is ConfidenceTier.CANDIDATE


def test_r4_suppressed_below_threshold():
    steps = _call_result("c0", "x", {}, {"ok": 1})
    report = _r4(steps)
    assert "no loop possible" in report.suppressions[0].suppressed_reason


# --- R5: redundant calls ---------------------------------------------------------------

def test_repeated_identical_call_with_work_between_is_redundant():
    steps = _call_result("c0", "get_profile", {"user": 9}, {"name": "A"})
    steps += _call_result("c1", "get_settings", {"user": 9}, {"theme": "dark"})
    steps += _call_result("c2", "get_profile", {"user": 9}, {"name": "A"})  # same as c0
    f = _r5(steps).active_findings[0]
    assert f.finding_type == "redundant_call"
    assert f.tier is ConfidenceTier.CANDIDATE
    assert f.step_indices == [0, 4]


def test_side_effecting_call_between_is_not_redundant():
    steps = _call_result("c0", "get_profile", {"user": 9}, {"name": "A"})
    steps += _call_result("c1", "update_profile", {"user": 9}, {"ok": True})
    steps += _call_result("c2", "get_profile", {"user": 9}, {"name": "A"})
    registry = ToolRegistry(
        {"update_profile": ToolSpec("update_profile", metadata=ToolMetadata(side_effecting=True))}
    )
    assert _r5(steps, registry).active_findings == []


def test_pagination_differs_by_args_not_flagged():
    steps = _call_result("c0", "list", {"page": 1}, {"items": [1]})
    steps += _call_result("c1", "list", {"page": 2}, {"items": [2]})
    assert _r5(steps).active_findings == []


def test_adjacent_identical_is_not_redundant_here():
    # Two adjacent identical calls are loop territory (R4), not R5.
    steps = _call_result("c0", "get", {"id": 1}, {"v": 1})
    steps += _call_result("c1", "get", {"id": 1}, {"v": 1})
    assert _r5(steps).active_findings == []


def test_r5_suppressed_with_one_call():
    report = _r5([ToolCall("c0", "x", {}), ToolResult("c0", {}, status=ResultStatus.OK)])
    assert "no repetition possible" in report.suppressions[0].suppressed_reason


# --- end to end ------------------------------------------------------------------------

def test_end_to_end_loop_demo():
    from tracelint.agent import run_loop_demo
    from tracelint.rules import default_rules

    trace, toolset = run_loop_demo()
    report = lint_trace(trace, default_rules(), toolset.to_registry())
    loops = [f for f in report.active_findings if f.finding_type == "loop"]
    assert len(loops) == 1 and loops[0].evidence["repeats"] == 3
    assert report.exit_code == 0  # a loop is a candidate, not a hard defect
