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


def test_empty_result_is_candidate():
    steps = [ToolCall("c1", "search", {}), ToolResult("c1", [], status=ResultStatus.UNKNOWN)]
    f = _r2a(steps).active_findings[0]
    assert f.evidence["signal"] == "empty_result"
    assert f.tier is ConfidenceTier.CANDIDATE


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
