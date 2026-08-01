"""Phase 3 — R3 hallucinated argument, tiered by schema annotation (spec §II.5, R3)."""

from __future__ import annotations

from tracelint import ConfidenceTier, ToolRegistry, ToolSpec, build_trace, lint_trace
from tracelint.rules import HallucinatedArgRule
from tracelint.trace import Message, ResultStatus, Role, ToolCall, ToolResult

# The spec's travel-booking case: reserve_flight fails, the agent sends a confirmation id that
# appears nowhere in its context.
TRAVEL_STEPS = [
    Message(Role.USER, "book me a flight to Austin"),
    ToolCall("c1", "reserve_flight", {"dest": "Austin"}),
    ToolResult("c1", "internal error", status=ResultStatus.ERROR, http_status=500),
    ToolCall("c2", "send_itinerary", {"confirmation_id": "CONF-4821"}),
]


def _lint(steps, registry=None):
    return lint_trace(build_trace("r", steps), [HallucinatedArgRule()], registry or ToolRegistry())


def test_unseen_arg_without_annotation_is_candidate():
    f = _lint(TRAVEL_STEPS).active_findings[0]
    assert f.finding_type == "hallucinated_arg"
    assert f.tier is ConfidenceTier.CANDIDATE
    assert f.possible_false_positive is True
    assert f.evidence["field"] == "confirmation_id"
    assert f.evidence["value"] == "CONF-4821"


def test_provided_annotation_makes_it_a_hard_defect():
    registry = ToolRegistry(
        {
            "send_itinerary": ToolSpec(
                "send_itinerary",
                schema={
                    "type": "object",
                    "properties": {
                        "confirmation_id": {"type": "string", "x-value-origin": "provided"}
                    },
                },
            )
        }
    )
    report = _lint(TRAVEL_STEPS, registry)
    f = report.active_findings[0]
    assert f.tier is ConfidenceTier.HARD_DEFECT
    assert report.exit_code == 2


def test_generated_annotation_is_skipped():
    # A request_id declared 'generated' (idempotency key) is legitimately not in context.
    registry = ToolRegistry(
        {
            "charge": ToolSpec(
                "charge",
                schema={
                    "type": "object",
                    "properties": {"request_id": {"type": "string", "x-value-origin": "generated"}},
                },
            )
        }
    )
    steps = [
        Message(Role.USER, "charge my card"),
        ToolCall("c1", "charge", {"request_id": "idem-a1b2c3d4"}),
    ]
    assert _lint(steps, registry).active_findings == []


def test_derivable_arg_is_not_flagged():
    steps = [
        Message(Role.USER, "cancel order 4521"),
        ToolCall("c1", "cancel_order", {"order_id": "4521"}),
    ]
    assert _lint(steps).active_findings == []


def test_enum_choice_is_not_flagged():
    registry = ToolRegistry(
        {
            "cancel_order": ToolSpec(
                "cancel_order",
                schema={
                    "type": "object",
                    "properties": {"reason": {"type": "string", "enum": ["fraud", "duplicate"]}},
                },
            )
        }
    )
    steps = [
        Message(Role.USER, "cancel it"),
        ToolCall("c1", "cancel_order", {"reason": "fraud"}),  # a choice, not a derived value
    ]
    assert _lint(steps, registry).active_findings == []


def test_boolean_and_nested_args_are_skipped():
    steps = [
        Message(Role.USER, "do the thing"),
        ToolCall("c1", "act", {"force": True, "opts": {"deep": "ZZZ-not-in-context"}}),
    ]
    assert _lint(steps).active_findings == []


def test_suppressed_without_tool_calls():
    report = _lint([Message(Role.USER, "hello")])
    assert report.suppressions[0].suppressed_reason == "trace has no tool calls to check"
