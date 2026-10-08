"""Phase 4 — R4 loop + R5 redundant call (spec §II.5, R4/R5; learning-doc 02 §4)."""

from __future__ import annotations

import pytest

from tracelint import (
    ConfidenceTier,
    ToolMetadata,
    ToolRegistry,
    ToolSpec,
    build_trace,
    lint_trace,
)
from tracelint.rules import LoopRule, RedundantCallRule
from tracelint.signatures import is_poll_call
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


def test_the_same_failing_command_with_the_same_error_is_a_loop():
    # A genuinely stuck agent (seen on a real run 494 times until the timeout).
    steps = []
    for i in range(5):
        steps += _call_result(
            f"c{i}", "Bash", {"command": "make"}, "make: *** No targets specified.",
            status=ResultStatus.ERROR,
        )
    (f,) = _r4(steps).active_findings
    assert f.evidence["repeats"] == 5 and f.evidence["poll"] is False
    assert "identical result" in f.summary


def test_a_repeat_whose_output_progresses_is_not_a_loop():
    # The same check returning a growing log / training progress: R4 used to compare only a coarse
    # "ok" class and read this as no change (55% of its loops on 10k real agent runs).
    steps = []
    for i, pct in enumerate((10, 40, 90)):
        steps += _call_result(f"c{i}", "Bash", {"command": "tail -n 1 train.log"}, f"epoch {pct}%")
    assert _r4(steps).active_findings == []


def test_identical_calls_with_no_recorded_result_are_disclosed_not_flagged():
    # A batch that returns one observation (Terminus), or a server-side tool: no result per call.
    steps = [ToolCall(f"c{i}", "web_search", {}) for i in range(3)]
    report = _r4(steps)
    assert report.active_findings == []
    (disclosure,) = report.suppressions
    assert disclosure.evidence["cause"] == "result_unrecorded"
    assert disclosure.step_indices == [0, 1, 2]


def test_a_poll_that_the_agent_moves_on_from_is_not_a_loop():
    wait = {"keystrokes": "", "duration": 30}
    steps = []
    for i in range(4):
        steps += _call_result(f"w{i}", "bash_command", wait, "New Terminal Output:\n")
    steps += _call_result("x", "bash_command", {"keystrokes": "ls\n"}, "a.py")
    assert _r4(steps).active_findings == []


def test_a_poll_still_unchanged_when_the_trace_ends_is_flagged():
    wait = {"keystrokes": "", "duration": 30}
    steps = []
    for i in range(4):
        steps += _call_result(f"w{i}", "bash_command", wait, "New Terminal Output:\n")
    (f,) = _r4(steps).active_findings
    assert f.evidence["poll"] is True and "ended still waiting" in f.summary


def test_a_tool_named_for_waiting_is_a_poll():
    steps = []
    for i in range(3):
        steps += _call_result(f"w{i}", "wait_shell_command", {"command_id": 6}, "still running")
    steps += _call_result("x", "read_file", {"path": "out.txt"}, "done")
    assert _r4(steps).active_findings == []


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


def test_undeclared_between_tool_is_disclosed_not_silently_assumed_inert():
    # get_settings is unknown to the registry: its side-effect status is unverifiable, so R5 still
    # surfaces the candidate (result is byte-identical) but discloses the undeclared tool.
    steps = _call_result("c0", "get_profile", {"user": 9}, {"name": "A"})
    steps += _call_result("c1", "get_settings", {"user": 9}, {"theme": "dark"})
    steps += _call_result("c2", "get_profile", {"user": 9}, {"name": "A"})
    f = _r5(steps).active_findings[0]
    assert f.evidence["undeclared_between"] == ["get_settings"]
    assert "unverified" in f.summary and "get_settings" in f.summary


def test_declared_safe_between_tool_is_not_flagged_as_undeclared():
    # get_settings is declared (side_effecting=False): known safe, so no "unverified" disclosure.
    steps = _call_result("c0", "get_profile", {"user": 9}, {"name": "A"})
    steps += _call_result("c1", "get_settings", {"user": 9}, {"theme": "dark"})
    steps += _call_result("c2", "get_profile", {"user": 9}, {"name": "A"})
    registry = ToolRegistry(
        {"get_settings": ToolSpec("get_settings", metadata=ToolMetadata(side_effecting=False))}
    )
    f = _r5(steps, registry).active_findings[0]
    assert f.evidence["undeclared_between"] == []
    assert "unverified" not in f.summary


def test_pagination_differs_by_args_not_flagged():
    steps = _call_result("c0", "list", {"page": 1}, {"items": [1]})
    steps += _call_result("c1", "list", {"page": 2}, {"items": [2]})
    assert _r5(steps).active_findings == []


def test_adjacent_identical_is_not_redundant_here():
    # Two adjacent identical calls are loop territory (R4), not R5.
    steps = _call_result("c0", "get", {"id": 1}, {"v": 1})
    steps += _call_result("c1", "get", {"id": 1}, {"v": 1})
    assert _r5(steps).active_findings == []


def test_repeats_with_no_recorded_result_are_disclosed_not_redundant():
    steps = [ToolCall("c0", "web_search", {})]
    steps += _call_result("c1", "get_settings", {"user": 9}, {"theme": "dark"})
    steps += [ToolCall("c2", "web_search", {})]
    report = _r5(steps)
    assert report.active_findings == []
    (disclosure,) = report.suppressions
    assert disclosure.evidence["cause"] == "result_unrecorded"


@pytest.mark.parametrize(
    ("name", "args", "poll"),
    [
        ("bash_command", {"keystrokes": "", "duration": 60}, True),  # terminal agent's wait
        ("write_stdin", {"session_id": 4, "chars": "", "summary": "Polling the build"}, True),
        ("Bash", {"command": "sleep 180 && tail build.log"}, True),
        ("wait_shell_command", {"command_id": 6}, True),
        ("pollJob", {"job": 7}, True),
        ("Bash", {"command": "make"}, False),
        ("web_search", {}, False),  # no input recorded: not evidence of a poll
        ("get_status", {"job": 7}, False),
    ],
)
def test_is_poll_call(name, args, poll):
    assert is_poll_call(ToolCall("c", name, args)) is poll


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
