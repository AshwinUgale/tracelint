"""R2b: a value comes from a failed call only if the failure is where the agent got it.

Up to 0.9, R2b read any later call that shared a value with an errored result as using it. A failed
result often echoes its inputs, so handling an error looked like misusing it, and CI failed on
correct runs: retrying a timed-out cancellation with the same subscription id, charging the backup
card the user named after a decline, or emailing a receipt alongside a fraud check that failed
(same customer id) were all hard defects. And only the first call sharing a value was examined, so
a harmless logging call hid the side-effecting transfer after it.

Now a value counts only if nothing else the agent observed supplies it, give or take case and
separators. Other failed results and calls the value was passed to are not independent sources.
Retries and recoveries of the failed tool are skipped, every later call is checked, and each misuse
is reported once.

``fixtures/r2b`` holds two real LangGraph runs (langgraph 1.2, openinference-instrumentation-
langchain 0.1.76), generated offline with a scripted chat model.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tracelint import (
    ToolRegistry,
    build_trace,
    default_rules,
    lint_trace,
    load_source,
)
from tracelint.findings import ConfidenceTier, Finding, LintReport
from tracelint.provenance import build_provenance
from tracelint.rules import ErrorHandlingRule
from tracelint.trace import Message, ResultStatus, Role, ToolCall, ToolResult

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "r2b"

OK, ERROR = ResultStatus.OK, ResultStatus.ERROR
STALE = {"error": "stale cache", "account": "ACC-STALE-9931"}


def _r2b(steps: list[Any], tools: dict[str, Any]) -> LintReport:
    registry = ToolRegistry.from_dict({"tools": tools})
    return lint_trace(build_trace("r", steps), [ErrorHandlingRule()], registry)


def _r2b_findings(report: LintReport) -> list[Finding]:
    return [f for f in report.active_findings if f.rule == "R2b"]


SIDE_EFFECTING = {"metadata": {"side_effecting": True}}


# --- Handling an error is not misusing it ------------------------------------------------------


def test_retrying_the_failed_call_is_handling_it():
    # Real LangGraph run: cancel_subscription times out ({"error": ..., "subscription_id": ...})
    # and the agent retries it with the id the user gave; the retry succeeds. 0.9: hard defect.
    (trace,) = load_source(FIXTURES / "langgraph_retry_spans.json", "openinference")
    registry = ToolRegistry.from_dict({"tools": {"cancel_subscription": SIDE_EFFECTING}})
    report = lint_trace(trace, default_rules(), registry)

    assert [(f.rule, f.tier) for f in report.active_findings] == [
        ("R2a", ConfidenceTier.HARD_EVENT)  # the timeout itself is still reported
    ]
    assert report.exit_code == 0


def test_a_parallel_call_with_the_users_values_is_not_fed_by_the_failure():
    # Real LangGraph run: the fraud check and the receipt go out in the same turn. The check fails
    # and echoes the customer id, but the receipt's id and email came from the user. What is left
    # is that the failed check was never retried: a candidate. 0.9: hard defect.
    (trace,) = load_source(FIXTURES / "langgraph_parallel_spans.json", "openinference")
    registry = ToolRegistry.from_dict(
        {"tools": {"check_fraud2": {}, "send_receipt": SIDE_EFFECTING}}
    )
    report = lint_trace(trace, default_rules(), registry)

    (finding,) = _r2b_findings(report)
    assert finding.tier is ConfidenceTier.CANDIDATE
    assert finding.evidence["signal"] == "not_retried"
    assert report.exit_code == 0


DECLINED = {
    "status": "declined",
    "order_id": "A100",
    "amount": 4999,
    "decline_code": "insufficient_funds",
}
CHARGE_CONTRACT = {
    "charge_card": {
        "metadata": {
            "side_effecting": True,
            "failure_when": {"pointer": "/status", "in": ["declined"]},
        }
    },
    "charge_backup_card": SIDE_EFFECTING,
}
BACKUP_REQUEST = Message(
    Role.USER,
    "Charge order A100 for 4999 cents; if my card is declined use my backup card pm_backup.",
)


def test_recharging_with_the_backup_card_the_user_named():
    # The charge is declined (it echoes the order and amount); the agent charges again with the
    # backup card the user gave. 0.9: hard defect.
    steps = [
        BACKUP_REQUEST,
        ToolCall("c1", "charge_card", {"order_id": "A100", "amount": 4999}),
        ToolResult("c1", DECLINED, status=OK),
        ToolCall(
            "c2", "charge_card", {"order_id": "A100", "amount": 4999, "payment_method": "pm_backup"}
        ),
        ToolResult("c2", {"status": "succeeded", "order_id": "A100", "amount": 4999}, status=OK),
    ]
    assert _r2b(steps, CHARGE_CONTRACT).active_findings == []


def test_recovery_through_another_tool_uses_the_users_values():
    # Same recovery through a different tool: the order and amount it shares with the decline
    # came from the user, so nothing was taken from the failure. 0.9: hard defect.
    steps = [
        BACKUP_REQUEST,
        ToolCall("c1", "charge_card", {"order_id": "A100", "amount": 4999}),
        ToolResult("c1", DECLINED, status=OK),
        ToolCall(
            "c2", "charge_backup_card", {"order_id": "A100", "amount": 4999, "card": "pm_backup"}
        ),
        ToolResult("c2", {"status": "succeeded"}, status=OK),
    ]
    report = _r2b(steps, CHARGE_CONTRACT)
    assert [f.evidence.get("signal") for f in report.active_findings] == ["not_retried"]
    assert report.exit_code == 0


@pytest.mark.parametrize(
    ("request_text", "order_id"),
    [
        ("Refund order A100.", "A100"),
        ("Refund order a-100, please.", "A100"),  # the agent normalized the id the user typed
        ("My order number is 123-456-789. Refund it.", "123456789"),
    ],
)
def test_an_id_the_user_gave_is_not_from_the_failure(request_text, order_id):
    # The behavior change: get_order fails and echoes the id, and the agent refunds that id anyway.
    # The id came from the user, so the refund did not use the failure's data. Whether a refund
    # needs a successful lookup first is a precondition, not dataflow: R2b reports the failure as
    # not retried (a candidate), and CI passes. 0.9: hard defect.
    steps = [
        Message(Role.USER, request_text),
        ToolCall("c1", "get_order", {"order_id": order_id}),
        ToolResult("c1", {"order_id": order_id, "status": "error"}, status=ERROR),
        ToolCall("c2", "refund_order", {"order_id": order_id}),
        ToolResult("c2", {"refunded": True}, status=OK),
    ]
    report = _r2b(steps, {"refund_order": SIDE_EFFECTING})

    (finding,) = report.active_findings
    assert finding.tier is ConfidenceTier.CANDIDATE
    assert finding.evidence["signal"] == "not_retried"
    assert report.exit_code == 0


def test_an_independent_lookup_supplies_the_value():
    # A fresh, successful lookup (not given the stale value) returns the same account: the
    # transfer's account came from it, not from the failure.
    steps = [
        Message(Role.USER, "Pay my landlord 1200."),
        ToolCall("c1", "lookup_account", {"payee": "landlord"}),
        ToolResult("c1", STALE, status=ERROR, error="stale cache"),
        ToolCall("c2", "get_payee", {"name": "landlord"}),
        ToolResult("c2", {"account": "ACC-STALE-9931", "verified": True}, status=OK),
        ToolCall("c3", "wire_transfer", {"account": "ACC-STALE-9931", "amount": 1200}),
        ToolResult("c3", {"sent": True}, status=OK),
    ]
    report = _r2b(steps, {"wire_transfer": SIDE_EFFECTING})
    assert [f.evidence.get("signal") for f in report.active_findings] == ["not_retried"]
    assert report.exit_code == 0


# --- Using a failure's data is still caught ----------------------------------------------------


def _refund_after_failed_lookup(*extra: Any) -> list[Any]:
    failed = {"order_id": "A100", "status": "error", "payment_method": "pm_7731"}
    return [
        Message(Role.USER, "Refund order A100 to the card on file."),
        ToolCall("c1", "get_order", {"order_id": "A100"}),
        ToolResult("c1", failed, status=ERROR),
        *extra,
        ToolCall("r1", "refund_order", {"order_id": "A100", "payment_method": "pm_7731"}),
        ToolResult("r1", {"refunded": True}, status=OK),
    ]


def test_a_value_only_the_failed_response_held_is_a_hard_defect():
    # The failed lookup still carried a cached payment method, and the refund went to it.
    report = _r2b(_refund_after_failed_lookup(), {"refund_order": SIDE_EFFECTING})

    (finding,) = report.active_findings
    assert finding.tier is ConfidenceTier.HARD_DEFECT
    assert finding.evidence["consumer"] == "refund_order"
    assert finding.evidence["consumed_values"] == ["pm_7731"]  # A100 came from the user
    assert report.exit_code == 2


def test_a_retry_that_fails_again_does_not_vouch_for_the_value():
    # The lookup is retried and fails again with the same cached payment method; each failure is
    # not an independent source for the other. One finding, against the first failure.
    failed_again = [
        ToolCall("c2", "get_order", {"order_id": "A100"}),
        ToolResult(
            "c2", {"order_id": "A100", "status": "error", "payment_method": "pm_7731"}, status=ERROR
        ),
    ]
    report = _r2b(_refund_after_failed_lookup(*failed_again), {"refund_order": SIDE_EFFECTING})

    (finding,) = report.active_findings
    assert finding.tier is ConfidenceTier.HARD_DEFECT
    assert finding.step_indices == [2, 5]
    assert report.exit_code == 2


def test_a_logging_call_does_not_hide_the_side_effect_after_it():
    # 0.9 examined only the first call to share the stale account, the log: a candidate.
    steps = [
        Message(Role.USER, "Pay my landlord 1200."),
        ToolCall("c1", "lookup_account", {"payee": "landlord"}),
        ToolResult("c1", STALE, status=ERROR, error="stale cache"),
        ToolCall("c2", "log_event", {"account": "ACC-STALE-9931", "event": "lookup_failed"}),
        ToolResult("c2", {"logged": True}, status=OK),
        ToolCall("c3", "wire_transfer", {"account": "ACC-STALE-9931", "amount": 1200}),
        ToolResult("c3", {"sent": True}, status=OK),
    ]
    report = _r2b(steps, {"wire_transfer": SIDE_EFFECTING})

    (finding,) = report.active_findings
    assert finding.tier is ConfidenceTier.HARD_DEFECT
    assert finding.evidence["consumer"] == "wire_transfer"
    assert finding.evidence["also_used_by"] == ["log_event"]
    assert finding.step_indices == [2, 5]


@pytest.mark.parametrize(
    ("tool", "result", "status", "tier"),
    [
        # A call the value was passed to hands it back: maybe a confirmation, maybe an echo.
        (
            "verify_account",
            {"account": "ACC-STALE-9931", "valid": True},
            OK,
            ConfidenceTier.CANDIDATE,
        ),
        ("log_event", {"logged": {"account": "ACC-STALE-9931"}}, OK, ConfidenceTier.CANDIDATE),
        # A verification that fails confirms nothing.
        (
            "verify_account",
            {"account": "ACC-STALE-9931", "valid": False},
            ERROR,
            ConfidenceTier.HARD_DEFECT,
        ),
    ],
)
def test_a_value_handed_back_by_a_call_it_was_passed_to(tool, result, status, tier):
    steps = [
        Message(Role.USER, "Pay my landlord 1200."),
        ToolCall("c1", "lookup_account", {"payee": "landlord"}),
        ToolResult("c1", STALE, status=ERROR, error="stale cache"),
        ToolCall("c2", tool, {"account": "ACC-STALE-9931"}),
        ToolResult("c2", result, status=status),
        ToolCall("c3", "wire_transfer", {"account": "ACC-STALE-9931", "amount": 1200}),
        ToolResult("c3", {"sent": True}, status=OK),
    ]
    report = _r2b(steps, {"wire_transfer": SIDE_EFFECTING})

    (finding,) = report.active_findings
    assert finding.evidence["consumer"] == "wire_transfer"
    assert finding.tier is tier
    if tier is ConfidenceTier.CANDIDATE:
        assert finding.evidence["echoed_by"] == [tool]
        assert f"{tool!r} returned the value when given it" in finding.summary
        assert report.exit_code == 0
    else:
        assert "echoed_by" not in finding.evidence
        assert report.exit_code == 2


# --- What counts as "available" ----------------------------------------------------------------


def test_strict_derivation_asks_whether_the_value_itself_was_seen():
    graph = build_provenance(
        [Message(Role.USER, "Ship to 221B Baker Street; card pm 7731; total 1,234.50; ref A-100")],
        up_to_index=1,
    )

    def available(value: str) -> bool:
        return graph.derive(value, strict=True).derivable

    # The same value, give or take case and separators, as a whole token.
    for value in ["221B Baker Street", "pm_7731", "1234.50", "a100", "Baker", "7731"]:
        assert available(value), value
    # Not part of a longer token, and not assembled from pieces.
    for value in ["aker", "12345", "7731-1234", "BakerStreet7731"]:
        assert not available(value), value
    # The lenient test still accepts digits found anywhere in the text.
    assert graph.derive("7731-1234").derivable
