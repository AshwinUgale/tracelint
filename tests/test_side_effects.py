"""R8 — duplicate side effect (Item 5).

A non-idempotent side-effecting tool called again with equivalent arguments, when the first call
did not fail, risks doing the effect twice (the double-charge). Tiered hard_event (first succeeded)
vs candidate (first outcome unknown); a repeat after a genuine failure is a legitimate retry.
"""

from __future__ import annotations

from tracelint import ConfidenceTier, ToolRegistry, Trace, lint_trace
from tracelint.rules import DuplicateSideEffectRule, rule_ids

_REG = ToolRegistry.from_dict(
    {
        "tools": {
            "charge": {"metadata": {"side_effecting": True}},  # idempotent defaults False
            "charge_idem": {"metadata": {"side_effecting": True, "idempotent": True}},
            "lookup": {"metadata": {}},  # a read tool
            "charge_fw": {
                "metadata": {
                    "side_effecting": True,
                    "failure_when": {"pointer": "/status", "in": ["declined"]},
                }
            },
        }
    }
)


def _steps(*pairs) -> dict:
    steps: list[dict] = []
    for cid, name, args, content, rkw in pairs:
        steps.append({"type": "tool_call", "call_id": cid, "name": name, "args": args})
        step = {"type": "tool_result", "call_id": cid, "content": content}
        step.update(rkw)
        steps.append(step)
    return {"run_id": "x", "steps": steps}


def _run(trace_dict: dict):
    return lint_trace(Trace.from_dict(trace_dict), [DuplicateSideEffectRule()], _REG)


def _r8(report):
    return [f for f in report.active_findings if f.rule == "R8"]


def test_repeat_after_success_is_a_hard_event():
    rep = _run(
        _steps(
            ("1", "charge", {"order": "A"}, {"ok": True}, {"status": "ok"}),
            ("2", "charge", {"order": "A"}, {"ok": True}, {"status": "ok"}),
        )
    )
    hits = _r8(rep)
    assert len(hits) == 1
    assert hits[0].tier is ConfidenceTier.HARD_EVENT
    assert hits[0].evidence["first_outcome"] == "success"
    assert rep.exit_code == 0  # a duplicate is an event, never fails CI on its own


def test_repeat_after_failure_is_not_flagged():
    rep = _run(
        _steps(
            ("1", "charge", {"order": "A"}, "boom", {"http_status": 500}),
            ("2", "charge", {"order": "A"}, {"ok": True}, {"status": "ok"}),
        )
    )
    assert _r8(rep) == []


def test_repeat_after_unknown_is_a_candidate():
    rep = _run(
        _steps(
            ("1", "charge", {"order": "A"}, {"queued": True}, {}),  # status defaults UNKNOWN
            ("2", "charge", {"order": "A"}, {"queued": True}, {}),
        )
    )
    hits = _r8(rep)
    assert len(hits) == 1
    assert hits[0].tier is ConfidenceTier.CANDIDATE
    assert hits[0].possible_false_positive is True
    assert hits[0].evidence["first_outcome"] == "unknown"


def test_idempotent_tool_is_not_flagged():
    rep = _run(
        _steps(
            ("1", "charge_idem", {"order": "A"}, {"ok": True}, {"status": "ok"}),
            ("2", "charge_idem", {"order": "A"}, {"ok": True}, {"status": "ok"}),
        )
    )
    assert _r8(rep) == []


def test_different_args_are_not_a_duplicate():
    rep = _run(
        _steps(
            ("1", "charge", {"order": "A"}, {"ok": True}, {"status": "ok"}),
            ("2", "charge", {"order": "B"}, {"ok": True}, {"status": "ok"}),
        )
    )
    assert _r8(rep) == []


def test_non_side_effecting_repeat_is_not_flagged():
    rep = _run(
        _steps(
            ("1", "lookup", {"id": "A"}, {"x": 1}, {"status": "ok"}),
            ("2", "lookup", {"id": "A"}, {"x": 1}, {"status": "ok"}),
        )
    )
    assert _r8(rep) == []


def test_failure_when_match_first_is_a_retry_not_a_duplicate():
    rep = _run(
        _steps(
            ("1", "charge_fw", {"order": "A"}, {"status": "declined"}, {"status": "ok"}),
            ("2", "charge_fw", {"order": "A"}, {"status": "confirmed"}, {"status": "ok"}),
        )
    )
    assert _r8(rep) == []  # first declared-failed → the repeat is a legitimate retry


def test_failure_when_no_match_first_is_a_duplicate():
    rep = _run(
        _steps(
            ("1", "charge_fw", {"order": "A"}, {"status": "confirmed"}, {"status": "ok"}),
            ("2", "charge_fw", {"order": "A"}, {"status": "confirmed"}, {"status": "ok"}),
        )
    )
    hits = _r8(rep)
    assert len(hits) == 1 and hits[0].tier is ConfidenceTier.HARD_EVENT


def test_r8_registered_in_defaults():
    assert "R8" in rule_ids()
