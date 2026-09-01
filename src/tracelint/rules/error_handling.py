"""R2 — Tool error handling (spec §II.5, R2; deep-design Trap 3).

Detecting an error is deterministic; detecting that the agent *mishandled* it usually is not, so
R2 is split into two rules with different confidence semantics:

**R2a — a tool returned an error** (``finding_type: tool_error_event``). Tiered by *how* the error
is expressed (Trap 3 — "what counts as an error is partly tool-specific"):
  - ``hard_event`` for **structured** signals only: an explicit ``status="error"``, an
    ``http_status >= 400``, or a structured ``error`` field. These are unambiguous.
  - ``candidate`` for **heuristics** on an otherwise-``unknown`` result: an exception-like string
    in free-form content (a search/docs tool may legitimately return text containing "Exception"),
    or an empty result. Flagged with ``possible_false_positive`` because they are not certain.
  R2a reports that an error *happened*; it is never a defect by itself, so it never fails CI.

**R2b — the error was improperly consumed / ignored** (``finding_type: error_mishandled``).
  - ``hard_defect`` (structurally provable): a value from a **structured-errored** result is reused
    as an argument to a later **side-effecting** tool call (metadata) — the agent fed data from a
    failed call into a real-world action with no fallback (the spec's ``send_itinerary`` case).
  - ``candidate`` otherwise: the same consumption into a non-side-effecting tool (could be
    legitimate error forwarding/logging), or a structured error the agent never retried before
    proceeding (judging whether a natural-language reply "acknowledged" it is not deterministic).

Without tool metadata, R2b cannot reach the hard tier — no ground truth, no hard verdict.
"""

from __future__ import annotations

import re
from typing import Any

from tracelint.findings import ConfidenceTier, Finding
from tracelint.predicates import PredicateResult
from tracelint.rules.base import Rule
from tracelint.signatures import is_structured_error as _is_structured_error
from tracelint.signatures import looks_empty as _looks_empty
from tracelint.tools import ToolRegistry
from tracelint.trace import ResultStatus, ToolResult, Trace
from tracelint.valueutil import significant_values as _significant_values

# Heuristic markers for an exception-like string in a free-form (unknown-status) result. Kept
# conservative — it only ever produces a *candidate* (possible false positive), so it favors the
# forms that actually show up in tool error strings ("Error:", "Failed to ...", a traceback).
_EXCEPTION_RE = re.compile(
    r"traceback \(most recent call last\)|\bexception\b|\b[A-Za-z]*Error\b|"
    r"\bfail(?:ed|ure)\b|http\s*[45]\d\d|\berrno\b",
    re.IGNORECASE,
)


def _exception_marker(content: Any) -> str | None:
    if isinstance(content, str):
        m = _EXCEPTION_RE.search(content)
        if m:
            return m.group(0)
    return None


class ToolErrorEventRule(Rule):
    """R2a: a tool returned an error (structured → hard_event, heuristic → candidate)."""

    id = "R2a"
    finding_type = "tool_error_event"

    def applicable(self, trace: Trace, registry: ToolRegistry) -> str | None:
        if not trace.tool_results():
            return "trace has no tool results"
        return None

    def run(self, trace: Trace, registry: ToolRegistry) -> list[Finding]:
        findings: list[Finding] = []
        for result in trace.tool_results():
            call = trace.call_for(result)
            tool = call.name if call else "?"
            meta = registry.metadata_for(tool) if call else None
            predicate = meta.failure_when if meta else None

            if _is_structured_error(result):
                findings.append(self._hard(result, tool))
                continue
            if predicate is not None:
                verdict = predicate.evaluate(result.content)
                if verdict is PredicateResult.MATCH:
                    findings.append(self._hard_predicate(result, tool, predicate))
                    continue
                # A declared failure_when whose field is *absent* can't be evaluated. On a
                # side-effecting tool that is not a clean pass — we cannot verify it did not fail —
                # so disclose it as a suppression, regardless of transport status (which may be OK).
                if verdict is PredicateResult.UNKNOWN and meta is not None and meta.side_effecting:
                    findings.append(self._suppress_unverifiable_predicate(result, tool, predicate))
                    continue
            # Fail-closed: a side-effecting action whose result we cannot classify (unknown status)
            # and that declares no failure predicate is *unverifiable* — we must not count it as a
            # clean pass. Disclose it as a suppression rather than assume success.
            if (
                meta is not None
                and meta.side_effecting
                and predicate is None
                and result.status is not ResultStatus.OK
            ):
                findings.append(self._suppress_unverified(result, tool))
                continue
            # Heuristics only on an unknown-status result — trust an explicit OK.
            if result.status is ResultStatus.OK:
                continue
            marker = _exception_marker(result.content)
            if marker is not None:
                findings.append(self._candidate(result, tool, "exception_text", marker))
            elif _looks_empty(result.content):
                findings.append(self._candidate(result, tool, "empty_result", ""))
        return findings

    def _hard_predicate(self, result: ToolResult, tool: str, predicate: Any) -> Finding:
        detail = predicate.describe(result.content)
        return Finding(
            rule=self.id,
            finding_type=self.finding_type,
            tier=ConfidenceTier.HARD_EVENT,
            summary=f"{tool!r} returned a declared failure ({detail})",
            evidence={
                "step_indices": [result.index],
                "tool": tool,
                "signal": "failure_predicate",
                "matched": detail,
            },
        )

    def _suppress_unverified(self, result: ToolResult, tool: str) -> Finding:
        reason = (
            f"side-effecting tool {tool!r} returned an unclassifiable result and declares no "
            "failure_when predicate — cannot verify it did not fail"
        )
        return Finding(
            rule=self.id,
            finding_type=self.finding_type,
            tier=ConfidenceTier.CANDIDATE,
            summary=f"rule {self.id} suppressed for {tool!r}: {reason}",
            evidence={"step_indices": [result.index], "tool": tool},
            suppressed_reason=reason,
        )

    def _suppress_unverifiable_predicate(
        self, result: ToolResult, tool: str, predicate: Any
    ) -> Finding:
        field = predicate.pointer or "(result)"
        reason = (
            f"declared failure_when field {field} absent on side-effecting tool {tool!r} — "
            "cannot verify it did not fail"
        )
        return Finding(
            rule=self.id,
            finding_type=self.finding_type,
            tier=ConfidenceTier.CANDIDATE,
            summary=f"rule {self.id} suppressed for {tool!r}: {reason}",
            evidence={
                "step_indices": [result.index],
                "tool": tool,
                "signal": "failure_predicate_unverifiable",
            },
            suppressed_reason=reason,
        )

    def _hard(self, result: ToolResult, tool: str) -> Finding:
        detail = (
            f"http {result.http_status}"
            if result.http_status is not None
            else (result.error or "status=error")
        )
        return Finding(
            rule=self.id,
            finding_type=self.finding_type,
            tier=ConfidenceTier.HARD_EVENT,
            summary=f"{tool!r} returned an error ({detail})",
            evidence={
                "step_indices": [result.index],
                "tool": tool,
                "signal": "structured",
                "http_status": result.http_status,
                "error": result.error,
            },
        )

    def _candidate(self, result: ToolResult, tool: str, signal: str, marker: str) -> Finding:
        why = (
            f"content matches an exception-like pattern ({marker!r})"
            if signal == "exception_text"
            else "result is empty"
        )
        return Finding(
            rule=self.id,
            finding_type=self.finding_type,
            tier=ConfidenceTier.CANDIDATE,
            summary=f"{tool!r} result may be an error — {why}",
            evidence={"step_indices": [result.index], "tool": tool, "signal": signal},
            possible_false_positive=True,
        )


class ErrorHandlingRule(Rule):
    """R2b: a structured error consumed by / ignored before a later action."""

    id = "R2b"
    finding_type = "error_mishandled"

    def applicable(self, trace: Trace, registry: ToolRegistry) -> str | None:
        if not trace.tool_results():
            return "trace has no tool results"
        return None

    def run(self, trace: Trace, registry: ToolRegistry) -> list[Finding]:
        findings: list[Finding] = []
        calls = trace.tool_calls()
        for result in trace.tool_results():
            errored_call = trace.call_for(result)
            meta = registry.metadata_for(errored_call.name) if errored_call else None
            predicate = meta.failure_when if meta else None
            declared_failure = predicate is not None and predicate.matches(result.content)
            if not (_is_structured_error(result) or declared_failure):
                continue
            err_vals = _significant_values(result.content) | _significant_values(result.error or "")

            consumer = None
            consumed: set[str] = set()
            for call in calls:
                if call.index <= result.index:
                    continue
                common = err_vals & _significant_values(call.args)
                if common:
                    consumer, consumed = call, common
                    break

            if consumer is not None:
                findings.append(
                    self._consumption(result, errored_call, consumer, consumed, registry)
                )
                continue

            # Not consumed — was the failing tool retried afterwards? If not, it may be ignored.
            failing = errored_call.name if errored_call else None
            retried = any(c.name == failing and c.index > result.index for c in calls)
            if not retried:
                findings.append(self._unhandled(result, failing))
        return findings

    def _consumption(self, result, errored_call, consumer, consumed, registry) -> Finding:
        meta = registry.metadata_for(consumer.name)
        is_side_effecting = bool(meta and meta.side_effecting)
        errored_tool = errored_call.name if errored_call else "?"
        values = ", ".join(sorted(consumed))
        return Finding(
            rule=self.id,
            finding_type=self.finding_type,
            tier=ConfidenceTier.HARD_DEFECT if is_side_effecting else ConfidenceTier.CANDIDATE,
            summary=(
                f"value(s) from the errored {errored_tool!r} result ({values}) reused as arguments "
                f"to {consumer.name!r}"
                + (" (a side-effecting action, no fallback)" if is_side_effecting else "")
            ),
            evidence={
                "step_indices": [result.index, consumer.index],
                "errored_tool": errored_tool,
                "consumer": consumer.name,
                "consumed_values": sorted(consumed),
                "side_effecting": is_side_effecting,
            },
            possible_false_positive=not is_side_effecting,
        )

    def _unhandled(self, result, failing) -> Finding:
        return Finding(
            rule=self.id,
            finding_type=self.finding_type,
            tier=ConfidenceTier.CANDIDATE,
            summary=(
                f"{failing!r} returned a structured error that was not retried before the agent "
                "proceeded (acknowledgement cannot be verified deterministically)"
            ),
            evidence={"step_indices": [result.index], "tool": failing, "signal": "not_retried"},
            possible_false_positive=True,
        )
