"""R12 — unresolved side effect: a side-effecting action the trace never resolved (audit 3.4).

Candidate / fail-closed: no recorded outcome, or a failure nothing recovered. Never a hard_defect —
a missing result could be a truncated capture — so it is shown for review and gates only under
`--fail-on candidate`. Opt-in: only side-effecting tools, judged on each tool's last call.
"""

from __future__ import annotations

from tracelint import ToolRegistry, build_trace, lint_trace
from tracelint.findings import ConfidenceTier
from tracelint.rules import rule_ids, select_rules
from tracelint.trace import ResultStatus, ToolCall, ToolResult

REG = ToolRegistry.from_dict(
    {"tools": {"charge": {"metadata": {"side_effecting": True}}, "get": {}}}
)


def _r12(*steps):
    return lint_trace(build_trace("t", list(steps)), select_rules(["R12"]), REG)


def test_r12_is_registered():
    assert "R12" in rule_ids()


def test_no_recorded_outcome_at_the_end_is_a_cautious_candidate():
    rep = _r12(ToolCall("c1", "charge", {"amt": 5}))  # a write with no result, as the last step
    (f,) = rep.active_findings
    assert f.rule == "R12" and f.tier is ConfidenceTier.CANDIDATE
    assert f.finding_type == "unresolved_side_effect"
    assert f.possible_false_positive is True  # could be a truncated capture


def test_no_outcome_but_the_run_continued_is_not_flagged_as_truncation():
    rep = _r12(
        ToolCall("c1", "charge", {"amt": 5}),
        ToolCall("c2", "get", {}),
        ToolResult("c2", {"ok": True}, status=ResultStatus.OK),
    )
    (f,) = rep.active_findings
    assert f.rule == "R12" and f.possible_false_positive is False  # capture worked after it


def test_a_failed_unrecovered_call_is_flagged():
    rep = _r12(
        ToolCall("c1", "charge", {"amt": 5}),
        ToolResult("c1", {"err": 1}, status=ResultStatus.ERROR),
    )
    (f,) = rep.active_findings
    assert f.rule == "R12" and "failed" in f.summary


def test_a_successful_side_effect_is_clean():
    rep = _r12(
        ToolCall("c1", "charge", {"amt": 5}),
        ToolResult("c1", {"ok": True}, status=ResultStatus.OK),
    )
    assert rep.active_findings == []


def test_a_later_success_recovers_an_earlier_failure():
    rep = _r12(
        ToolCall("c1", "charge", {"amt": 5}),
        ToolResult("c1", {"err": 1}, status=ResultStatus.ERROR),
        ToolCall("c2", "charge", {"amt": 5}),
        ToolResult("c2", {"ok": True}, status=ResultStatus.OK),
    )
    assert rep.active_findings == []


def test_a_non_side_effecting_tool_is_ignored():
    assert _r12(ToolCall("c1", "get", {})).active_findings == []


def test_candidate_is_hidden_by_default_and_gates_only_under_fail_on():
    rep = _r12(ToolCall("c1", "charge", {"amt": 5}))
    assert rep.exit_code == 0  # candidate: never a default CI failure
    rep.fail_on = ConfidenceTier.CANDIDATE
    assert rep.exit_code == 1
