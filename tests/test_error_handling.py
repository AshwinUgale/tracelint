"""Phase 2 — R2a tool-error events + R2b error mishandling (spec §II.5, R2)."""

from __future__ import annotations

from tracelint import (
    ConfidenceTier,
    ToolMetadata,
    ToolRegistry,
    ToolSpec,
    build_trace,
    lint_trace,
)
from tracelint.rules import ErrorHandlingRule, ToolErrorEventRule
from tracelint.trace import Message, ResultStatus, Role, ToolCall, ToolResult


def _r2a(steps, registry=None):
    return lint_trace(build_trace("r", steps), [ToolErrorEventRule()], registry or ToolRegistry())


def _r2b(steps, registry=None):
    return lint_trace(build_trace("r", steps), [ErrorHandlingRule()], registry or ToolRegistry())


# --- R2a: event detection --------------------------------------------------------------


def _reg(name, **meta):
    return ToolRegistry({name: ToolSpec(name, metadata=ToolMetadata.from_dict(meta))})


def test_declared_failure_predicate_is_hard_event():
    # A decline arriving as transport success ({"status":"declined"}) is caught once declared.
    steps = [
        ToolCall("c1", "charge", {}),
        ToolResult("c1", {"status": "declined"}, status=ResultStatus.UNKNOWN),
    ]
    reg = _reg("charge", failure_when={"pointer": "/status", "in": ["declined", "failed"]})
    f = _r2a(steps, reg).active_findings[0]
    assert f.tier is ConfidenceTier.HARD_EVENT
    assert f.evidence["signal"] == "failure_predicate"
    assert "declined" in f.evidence["matched"]


def test_predicate_not_matched_is_clean():
    steps = [
        ToolCall("c1", "charge", {}),
        ToolResult("c1", {"status": "approved"}, status=ResultStatus.OK),
    ]
    reg = _reg("charge", failure_when={"pointer": "/status", "in": ["declined"]})
    assert _r2a(steps, reg).active_findings == []


def test_side_effecting_without_predicate_is_suppressed_not_clean():
    # Fail-closed: an unclassifiable side-effecting result with no predicate is disclosed.
    steps = [
        ToolCall("c1", "charge", {}),
        ToolResult("c1", {"status": "declined"}, status=ResultStatus.UNKNOWN),
    ]
    report = _r2a(steps, _reg("charge", side_effecting=True))
    assert report.active_findings == []
    assert "cannot verify it did not fail" in report.suppressions[0].suppressed_reason


def test_explicit_ok_side_effecting_is_trusted_no_suppression():
    # An explicit OK is trusted — the suppression is only for unclassifiable (unknown) results.
    steps = [
        ToolCall("c1", "charge", {}),
        ToolResult("c1", {"ok": True}, status=ResultStatus.OK),
    ]
    report = _r2a(steps, _reg("charge", side_effecting=True))
    assert report.active_findings == [] and report.suppressions == []


def test_predicate_failure_reused_into_side_effecting_is_hard_defect():
    # R2b: a declared-failure value flowing into a later side-effecting call is a hard defect.
    steps = [
        ToolCall("c1", "charge", {}),
        ToolResult("c1", {"status": "declined", "ref": "DECL9"}, status=ResultStatus.UNKNOWN),
        ToolCall("c2", "send_receipt", {"ref": "DECL9"}),
        ToolResult("c2", {"sent": True}, status=ResultStatus.OK),
    ]
    reg = ToolRegistry.from_dict(
        {
            "tools": {
                "charge": {
                    "metadata": {"failure_when": {"pointer": "/status", "equals": "declined"}}
                },
                "send_receipt": {"metadata": {"side_effecting": True}},
            }
        }
    )
    f = _r2b(steps, reg).active_findings[0]
    assert f.tier is ConfidenceTier.HARD_DEFECT
    assert f.finding_type == "error_mishandled"


def test_structured_status_error_is_hard_event():
    steps = [
        ToolCall("c1", "reserve", {}),
        ToolResult("c1", "boom", status=ResultStatus.ERROR),
    ]
    f = _r2a(steps).active_findings[0]
    assert f.tier is ConfidenceTier.HARD_EVENT
    assert f.finding_type == "tool_error_event"
    assert f.evidence["signal"] == "structured"


def test_http_400plus_is_hard_event():
    steps = [
        ToolCall("c1", "reserve", {}),
        ToolResult("c1", "nope", status=ResultStatus.UNKNOWN, http_status=503),
    ]
    assert _r2a(steps).active_findings[0].tier is ConfidenceTier.HARD_EVENT


def test_exception_text_in_unknown_result_is_candidate():
    steps = [
        ToolCall("c1", "search", {}),
        ToolResult(
            "c1", "Traceback (most recent call last): ValueError", status=ResultStatus.UNKNOWN
        ),
    ]
    f = _r2a(steps).active_findings[0]
    assert f.tier is ConfidenceTier.CANDIDATE
    assert f.possible_false_positive is True
    assert f.evidence["signal"] == "exception_text"


def test_failed_text_in_unknown_result_is_candidate():
    # "Failed to ..." is a common tool error string; the heuristic now catches it (candidate).
    steps = [
        ToolCall("c1", "call_api", {}),
        ToolResult("c1", "Failed to connect to upstream", status=ResultStatus.UNKNOWN),
    ]
    f = _r2a(steps).active_findings[0]
    assert f.tier is ConfidenceTier.CANDIDATE
    assert f.evidence["signal"] == "exception_text"


def test_free_text_error_via_contains_predicate_is_hard_event():
    # The MCP case: a tool reports failure as a plain "Error: ..." string over an unknown status.
    # A declared contains/matches predicate turns that into a structural hard event.
    steps = [
        ToolCall("c1", "mcp_tool", {}),
        ToolResult("c1", "Error: upstream returned 500", status=ResultStatus.UNKNOWN),
    ]
    reg = _reg("mcp_tool", failure_when={"pointer": "", "contains": "Error:"})
    f = _r2a(steps, reg).active_findings[0]
    assert f.tier is ConfidenceTier.HARD_EVENT
    assert f.evidence["signal"] == "failure_predicate"


def test_empty_result_is_candidate():
    steps = [ToolCall("c1", "search", {}), ToolResult("c1", [], status=ResultStatus.UNKNOWN)]
    f = _r2a(steps).active_findings[0]
    assert f.evidence["signal"] == "empty_result"
    assert f.tier is ConfidenceTier.CANDIDATE


def test_status_error_convention_without_contract_is_candidate():
    # An HTTP-200 error envelope ({"status":"error"}) with no declared failure_when is a hint, not
    # a fact — surfaced as a candidate that names the fix, even though transport looks OK.
    steps = [
        ToolCall("c1", "get_order", {"order_id": "A100"}),
        ToolResult("c1", {"status": "error", "code": 500}, status=ResultStatus.UNKNOWN),
    ]
    f = _r2a(steps).active_findings[0]
    assert f.tier is ConfidenceTier.CANDIDATE
    assert f.possible_false_positive is True
    assert f.evidence["signal"] == "status_convention"
    assert "failure_when" in f.summary


def test_status_error_convention_yields_to_declared_contract():
    # When a failure_when contract exists, the contract decides (hard event) — the convention
    # candidate must not also fire, so exactly one finding, at the hard tier.
    steps = [
        ToolCall("c1", "get_order", {"order_id": "A100"}),
        ToolResult("c1", {"status": "error", "code": 500}, status=ResultStatus.UNKNOWN),
    ]
    reg = _reg("get_order", failure_when={"pointer": "/status", "in": ["error"]})
    findings = _r2a(steps, reg).active_findings
    assert len(findings) == 1
    assert findings[0].tier is ConfidenceTier.HARD_EVENT


def test_nested_failed_status_is_not_flagged_by_convention():
    # A retrieved failed *item* ({"jobs":[{"status":"failed"}]}) does not mean the call failed —
    # the convention is top-level only, so nothing fires.
    steps = [
        ToolCall("c1", "list_jobs", {}),
        ToolResult("c1", {"jobs": [{"status": "failed"}]}, status=ResultStatus.UNKNOWN),
    ]
    assert _r2a(steps).active_findings == []


def test_explicit_ok_is_trusted_no_heuristic_flag():
    # An OK result containing the word "Exception" (e.g. a docs tool) is NOT flagged.
    steps = [
        ToolCall("c1", "read_docs", {}),
        ToolResult("c1", "How to handle an Exception in Python", status=ResultStatus.OK),
    ]
    assert _r2a(steps).active_findings == []


def test_r2a_event_does_not_fail_ci():
    steps = [ToolCall("c1", "reserve", {}), ToolResult("c1", "x", status=ResultStatus.ERROR)]
    report = _r2a(steps)
    assert report.exit_code == 0  # an event is not a defect


def test_r2a_suppressed_without_results():
    report = _r2a([Message(Role.USER, "hi")])
    assert report.suppressions[0].suppressed_reason == "trace has no tool results"


# --- R2b: mishandling ------------------------------------------------------------------


def _side_effecting_registry() -> ToolRegistry:
    return ToolRegistry(
        {"send_itinerary": ToolSpec("send_itinerary", metadata=ToolMetadata(side_effecting=True))}
    )


def test_errored_value_into_side_effecting_call_is_hard_defect():
    # reserve fails but its (garbage) confirmation is reused in a side-effecting send.
    steps = [
        ToolCall("c1", "reserve_flight", {}),
        ToolResult(
            "c1", {"confirmation_id": "CONF-4821"}, status=ResultStatus.ERROR, http_status=500
        ),
        ToolCall("c2", "send_itinerary", {"confirmation_id": "CONF-4821"}),
        ToolResult("c2", {"sent": True}, status=ResultStatus.OK),
    ]
    report = _r2b(steps, _side_effecting_registry())
    f = report.active_findings[0]
    assert f.tier is ConfidenceTier.HARD_DEFECT
    assert report.exit_code == 2
    assert f.evidence["consumed_values"] == ["CONF-4821"]
    assert set(f.step_indices) == {1, 2}


def test_consumption_into_non_side_effecting_is_candidate():
    # Same reuse, but the consumer is not declared side-effecting → candidate, not hard.
    steps = [
        ToolCall("c1", "reserve_flight", {}),
        ToolResult("c1", {"confirmation_id": "CONF-4821"}, status=ResultStatus.ERROR),
        ToolCall("c2", "log_event", {"confirmation_id": "CONF-4821"}),
    ]
    report = _r2b(steps, ToolRegistry())  # no metadata → cannot reach hard tier
    f = report.active_findings[0]
    assert f.tier is ConfidenceTier.CANDIDATE
    assert f.possible_false_positive is True
    assert report.exit_code == 0


def test_unretried_error_is_candidate_ignored():
    steps = [
        ToolCall("c1", "get_status", {"id": "9999"}),
        ToolResult("c1", "server error", status=ResultStatus.ERROR),
        Message(Role.ASSISTANT, "All done!"),
    ]
    f = _r2b(steps).active_findings[0]
    assert f.tier is ConfidenceTier.CANDIDATE
    assert f.evidence["signal"] == "not_retried"


def test_retried_error_is_not_flagged():
    steps = [
        ToolCall("c1", "get_status", {"id": "9999"}),
        ToolResult("c1", "server error", status=ResultStatus.ERROR),
        ToolCall("c2", "get_status", {"id": "9999"}),  # a retry = handling
        ToolResult("c2", {"ok": True}, status=ResultStatus.OK),
    ]
    assert _r2b(steps).active_findings == []


def test_no_error_no_finding():
    steps = [
        ToolCall("c1", "get_status", {"id": "9999"}),
        ToolResult("c1", {"status": "ok"}, status=ResultStatus.OK),
    ]
    assert _r2b(steps).active_findings == []


def test_end_to_end_agent_ignored_errors_are_detected():
    from tracelint.agent import run_ignored_error_demo
    from tracelint.rules import default_rules

    trace, toolset = run_ignored_error_demo()
    report = lint_trace(trace, default_rules(), toolset.to_registry())
    # Both tool calls errored (404): R2a raises two hard_events...
    events = [f for f in report.active_findings if f.finding_type == "tool_error_event"]
    assert len(events) == 2 and all(f.tier is ConfidenceTier.HARD_EVENT for f in events)
    # ...and R2b flags the unretried/ignored errors as candidates (not deterministic → candidate).
    mishandled = [f for f in report.active_findings if f.finding_type == "error_mishandled"]
    assert mishandled and all(f.tier is ConfidenceTier.CANDIDATE for f in mishandled)
    # No hard_defect here (nothing consumed into a side-effecting call), so CI stays green.
    assert report.exit_code == 0


def test_trivial_values_do_not_ground_consumption():
    # A short shared value ("ok") must not be treated as a consumed field.
    steps = [
        ToolCall("c1", "reserve", {}),
        ToolResult("c1", {"note": "ok"}, status=ResultStatus.ERROR),
        ToolCall("c2", "send", {"note": "ok"}),
    ]
    report = _r2b(steps, _side_effecting_registry())
    # No hard consumption on the trivial value; falls through to the "not retried" candidate.
    assert not report.has_hard_defect
