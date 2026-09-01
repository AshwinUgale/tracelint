"""Tri-state failure_when semantics (issue #34).

A declared value predicate must distinguish "field present, not a failure value" (a clean pass)
from "field absent" (cannot verify). The latter must never become a silent clean pass on a
side-effecting tool — that would let an API dropping the field sail through the exact contract
written to catch its failure.
"""

from __future__ import annotations

from tracelint import ConfidenceTier, ToolRegistry, Trace, lint_trace
from tracelint.predicates import FailurePredicate, PredicateResult
from tracelint.rules import ToolErrorEventRule

# --- predicate layer ---------------------------------------------------------------


def _status_pred(**kw: object) -> FailurePredicate:
    pred = FailurePredicate.from_dict({"pointer": "/status", **kw})
    assert pred is not None
    return pred


def test_value_predicate_is_tristate():
    p = _status_pred(**{"in": ["declined", "failed"]})
    assert p.evaluate({"status": "declined"}) is PredicateResult.MATCH
    assert p.evaluate({"status": "succeeded"}) is PredicateResult.NO_MATCH  # present, not a failure
    assert p.evaluate({"amount": 50}) is PredicateResult.UNKNOWN  # /status absent → can't decide


def test_optional_makes_absent_a_clean_no_match():
    p = _status_pred(**{"in": ["declined"], "optional": True})
    assert p.evaluate({"amount": 50}) is PredicateResult.NO_MATCH


def test_pure_exists_treats_absence_as_no_match():
    p = FailurePredicate.from_dict({"pointer": "/error_code", "exists": True})
    assert p is not None
    assert p.evaluate({"error_code": "E1"}) is PredicateResult.MATCH  # present ⇒ failure
    assert p.evaluate({"ok": True}) is PredicateResult.NO_MATCH  # absent existence check = clean


def test_matches_shim_is_match_only():
    p = _status_pred(**{"in": ["declined"]})
    assert p.matches({"status": "declined"}) is True
    assert p.matches({"status": "ok"}) is False  # NO_MATCH
    assert p.matches({"no": "status"}) is False  # UNKNOWN is not a match (back-compat preserved)


# --- rule layer (R2a) --------------------------------------------------------------

_REG = ToolRegistry.from_dict(
    {
        "tools": {
            "charge_card": {
                "metadata": {
                    "side_effecting": True,
                    "failure_when": {"pointer": "/status", "in": ["declined", "failed"]},
                }
            }
        }
    }
)


def _trace(content: object) -> Trace:
    return Trace.from_dict(
        {
            "run_id": "x",
            "steps": [
                {
                    "type": "tool_call",
                    "call_id": "c1",
                    "name": "charge_card",
                    "args": {"amount": 50},
                },
                {"type": "tool_result", "call_id": "c1", "content": content, "status": "ok"},
            ],
        }
    )


def _report(content: object, reg: ToolRegistry = _REG):
    return lint_trace(_trace(content), [ToolErrorEventRule()], reg)


def test_declined_still_hard_event():
    rep = _report({"status": "declined"})
    assert any(f.tier is ConfidenceTier.HARD_EVENT for f in rep.active_findings)


def test_succeeded_is_a_clean_pass():
    rep = _report({"status": "succeeded"})
    assert not rep.active_findings and not rep.suppressions


def test_absent_field_is_a_suppression_not_a_clean_pass():
    rep = _report({"amount": 50, "receipt": "R1"})  # /status dropped by the API
    assert not rep.active_findings  # not asserted as a defect...
    assert len(rep.suppressions) == 1  # ...but NOT a silent clean pass either
    reason = rep.suppressions[0].suppressed_reason or ""
    assert "cannot verify it did not fail" in reason
    assert "/status" in reason


def test_optional_field_absent_is_a_clean_pass():
    reg = ToolRegistry.from_dict(
        {
            "tools": {
                "charge_card": {
                    "metadata": {
                        "side_effecting": True,
                        "failure_when": {
                            "pointer": "/status",
                            "in": ["declined"],
                            "optional": True,
                        },
                    }
                }
            }
        }
    )
    rep = _report({"amount": 50}, reg)
    assert not rep.active_findings and not rep.suppressions  # declared optional → clean


def test_absent_field_on_read_tool_is_not_suppressed():
    # The UNKNOWN suppression is scoped to side-effecting tools; a read tool falls through.
    reg = ToolRegistry.from_dict(
        {
            "tools": {
                "get_status": {"metadata": {"failure_when": {"pointer": "/status", "in": ["x"]}}}
            }
        }
    )
    trace = Trace.from_dict(
        {
            "run_id": "x",
            "steps": [
                {"type": "tool_call", "call_id": "c1", "name": "get_status", "args": {}},
                {"type": "tool_result", "call_id": "c1", "content": {"data": 1}, "status": "ok"},
            ],
        }
    )
    rep = lint_trace(trace, [ToolErrorEventRule()], reg)
    assert not rep.active_findings and not rep.suppressions
