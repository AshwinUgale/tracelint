"""Constructed validation suite (spec §II.11; cross-tool principle #4).

A diagnostic must be validated on constructed ground truth: hand-authored traces with exactly one
planted instance of each defect, clean controls that must stay silent, and legitimate-but-suspicious
cases that must emit a **candidate, not a verdict** (a real retry, a legitimate value transform, a
generated idempotency key). Each :class:`ValidationCase` carries a ``check`` that encodes the
expected behaviour, so the same cases drive the test suite, the ``demo`` command, and the HTML
report — the tool proving, on itself, that it recovers what it should and stays quiet otherwise.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from tracelint.agent import run_demo, run_loop_demo
from tracelint.findings import ConfidenceTier, LintReport
from tracelint.tools import ToolMetadata, ToolRegistry, ToolSpec
from tracelint.trace import Message, ResultStatus, Role, ToolCall, ToolResult, Trace, build_trace

Check = Callable[[LintReport], bool]


@dataclass
class ValidationCase:
    name: str
    kind: str  # "planted" | "control" | "suspicious"
    description: str
    trace: Trace
    registry: ToolRegistry
    expectation: str
    check: Check


# --- check builders --------------------------------------------------------------------

def _has(rule: str, tier: ConfidenceTier | None = None) -> Check:
    return lambda rep: any(
        f.rule == rule and (tier is None or f.tier is tier) for f in rep.active_findings
    )


def _and(*checks: Check) -> Check:
    return lambda rep: all(c(rep) for c in checks)


def _silent() -> Check:
    return lambda rep: len(rep.active_findings) == 0


def _no_rule(rule: str) -> Check:
    return lambda rep: not any(f.rule == rule for f in rep.active_findings)


def _no_hard_defect() -> Check:
    return lambda rep: not rep.has_hard_defect


# --- schemas / registries --------------------------------------------------------------

_CANCEL_SCHEMA = {
    "type": "object",
    "properties": {
        "order_id": {"type": "string", "pattern": "^[0-9]{3,10}$"},
        "reason": {"type": "string", "enum": ["customer_request", "duplicate", "fraud"]},
    },
    "required": ["order_id", "reason"],
    "additionalProperties": False,
}


def _reg(*specs: ToolSpec) -> ToolRegistry:
    return ToolRegistry({s.name: s for s in specs})


# --- cases -----------------------------------------------------------------------------

def validation_cases() -> list[ValidationCase]:
    cases: list[ValidationCase] = []

    # R1 — schema violation (planted): order_id emitted as an integer.
    cases.append(
        ValidationCase(
            "r1_schema_violation", "planted",
            "A tool call whose arguments violate the declared JSON Schema (int, not string).",
            build_trace("v-r1", [
                Message(Role.USER, "cancel order 4521 as fraud"),
                ToolCall("c1", "cancel_order", {"order_id": 4521, "reason": "fraud"}),
            ]),
            _reg(ToolSpec("cancel_order", schema=_CANCEL_SCHEMA)),
            "R1 hard_defect", _has("R1", ConfidenceTier.HARD_DEFECT),
        )
    )

    # R2a — tool error event (planted): a structured HTTP 500.
    cases.append(
        ValidationCase(
            "r2a_tool_error", "planted",
            "A tool returns a structured error (HTTP 500) — a hard event.",
            build_trace("v-r2a", [
                Message(Role.USER, "reserve a flight"),
                ToolCall("c1", "reserve_flight", {"dest": "Austin"}),
                ToolResult("c1", "error", status=ResultStatus.ERROR, http_status=500),
                Message(Role.ASSISTANT, "Sorry, I hit an error."),
            ]),
            _reg(ToolSpec("reserve_flight")),
            "R2a hard_event", _has("R2a", ConfidenceTier.HARD_EVENT),
        )
    )

    # R2b — error improperly consumed (planted): a failed call's field fed to a side-effecting tool.
    cases.append(
        ValidationCase(
            "r2b_error_consumed", "planted",
            "A value from an errored result is reused as an argument to a side-effecting call.",
            build_trace("v-r2b", [
                Message(Role.USER, "book me a flight to Austin"),
                ToolCall("c1", "reserve_flight", {"dest": "Austin"}),
                ToolResult(
                    "c1", {"confirmation_id": "CONF-4821"},
                    status=ResultStatus.ERROR, http_status=500,
                ),
                ToolCall("c2", "send_itinerary", {"confirmation_id": "CONF-4821"}),
                ToolResult("c2", {"sent": True}, status=ResultStatus.OK),
            ]),
            _reg(ToolSpec("send_itinerary", metadata=ToolMetadata(side_effecting=True))),
            "R2b hard_defect", _has("R2b", ConfidenceTier.HARD_DEFECT),
        )
    )

    # R3 — hallucinated arg, candidate (planted, unannotated schema).
    travel = build_trace("v-r3", [
        Message(Role.USER, "book me a flight to Austin"),
        ToolCall("c1", "reserve_flight", {"dest": "Austin"}),
        ToolResult("c1", "error", status=ResultStatus.ERROR, http_status=500),
        ToolCall("c2", "send_itinerary", {"confirmation_id": "CONF-4821"}),
    ])
    cases.append(
        ValidationCase(
            "r3_hallucination_candidate", "planted",
            "An argument absent from provenance, with no schema annotation → candidate.",
            travel, _reg(ToolSpec("send_itinerary")),
            "R3 candidate (not hard)",
            _and(_has("R3", ConfidenceTier.CANDIDATE), _no_hard_defect()),
        )
    )

    # R3 — hallucinated arg, hard (planted, annotated x-value-origin: provided).
    cases.append(
        ValidationCase(
            "r3_hallucination_hard", "planted",
            "The same absent argument, but the schema declares the field 'provided' → hard_defect.",
            travel,
            _reg(ToolSpec("send_itinerary", schema={
                "type": "object",
                "properties": {"confirmation_id": {"type": "string", "x-value-origin": "provided"}},
            })),
            "R3 hard_defect", _has("R3", ConfidenceTier.HARD_DEFECT),
        )
    )

    # R4 — loop (planted): the agent repeats the same failing lookup (demo scenario).
    loop_trace, loop_toolset = run_loop_demo()
    cases.append(
        ValidationCase(
            "r4_loop", "planted",
            "The agent repeats an identical failing call with no progress → loop candidate.",
            loop_trace, loop_toolset.to_registry(),
            "R4 loop candidate", _has("R4", ConfidenceTier.CANDIDATE),
        )
    )

    # R5 — redundant call (planted): identical read repeated with work but no mutation between.
    cases.append(
        ValidationCase(
            "r5_redundant", "planted",
            "An identical read repeated after unrelated work, with no mutation between.",
            build_trace("v-r5", [
                ToolCall("c0", "get_profile", {"user": 9}),
                ToolResult("c0", {"name": "A"}, status=ResultStatus.OK),
                ToolCall("c1", "get_settings", {"user": 9}),
                ToolResult("c1", {"theme": "dark"}, status=ResultStatus.OK),
                ToolCall("c2", "get_profile", {"user": 9}),
                ToolResult("c2", {"name": "A"}, status=ResultStatus.OK),
            ]),
            ToolRegistry(),
            "R5 redundant candidate", _has("R5", ConfidenceTier.CANDIDATE),
        )
    )

    # Clean control: a correct run must stay silent.
    clean_trace, clean_toolset = run_demo()
    cases.append(
        ValidationCase(
            "clean_control", "control",
            "A correct order-cancellation run — the linter must be silent.",
            clean_trace, clean_toolset.to_registry(),
            "no active findings", _silent(),
        )
    )

    # Suspicious 1 — a real retry (error then success): must NOT be flagged as a loop or defect.
    cases.append(
        ValidationCase(
            "suspicious_retry", "suspicious",
            "A transient error followed by a successful retry — legitimate, not a loop.",
            build_trace("v-retry", [
                ToolCall("c0", "get_status", {"id": "9"}),
                ToolResult("c0", "temporary error", status=ResultStatus.ERROR, http_status=503),
                ToolCall("c1", "get_status", {"id": "9"}),
                ToolResult("c1", {"status": "ok"}, status=ResultStatus.OK),
            ]),
            ToolRegistry(),
            "no loop, no hard_defect", _and(_no_rule("R4"), _no_hard_defect()),
        )
    )

    # Suspicious 2 — a legitimate value transform (digit reformat) must NOT be a hallucination.
    cases.append(
        ValidationCase(
            "suspicious_transform", "suspicious",
            "A legitimate reformat of a user-provided value (1,234.56 becomes 1234.56).",
            build_trace("v-transform", [
                Message(Role.USER, "charge the total of 1,234.56 dollars"),
                ToolCall("c1", "charge", {"amount": "1234.56"}),
            ]),
            _reg(ToolSpec("charge")),
            "no R3", _no_rule("R3"),
        )
    )

    # Suspicious 3 — a generated idempotency key (annotated) must NOT be a hallucination.
    cases.append(
        ValidationCase(
            "suspicious_generated_key", "suspicious",
            "An idempotency key absent from context but declared 'generated' → not flagged.",
            build_trace("v-idem", [
                Message(Role.USER, "charge my card"),
                ToolCall("c1", "charge", {"request_id": "idem-a1b2c3d4e5"}),
            ]),
            _reg(ToolSpec("charge", schema={
                "type": "object",
                "properties": {"request_id": {"type": "string", "x-value-origin": "generated"}},
            })),
            "no R3", _no_rule("R3"),
        )
    )

    return cases
