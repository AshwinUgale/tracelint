"""R2 — Tool error handling (spec §II.5, R2; deep-design Trap 3).

Detecting an error is deterministic; detecting that the agent *mishandled* it usually is not, so
R2 is split into two rules with different confidence semantics:

**R2a — a tool returned an error** (``finding_type: tool_error_event``). Tiered by *how* the error
is expressed (Trap 3 — "what counts as an error is partly tool-specific"):
  - ``hard_event`` for **structured** signals only: an explicit ``status="error"``, an
    ``http_status >= 400``, or a structured ``error`` field. These are unambiguous.
  - ``candidate`` for **heuristics** on an otherwise-``unknown`` result: an error the output
    *reports* — a line that starts with ``ERROR:`` / ``fatal:`` / ``ValueError:`` / a traceback /
    ``bash: x: command not found`` (an error word inside other text, such as a viewed file's
    ``raise ValueError``, is not one) — or an empty result, unless the call was a search that found
    nothing. Flagged with ``possible_false_positive`` because they are not certain.
  R2a reports that an error *happened*; it is never a defect by itself, so it never fails CI.

**R2b — the error was improperly consumed / ignored** (``finding_type: error_mishandled``).
  - ``hard_defect`` (structurally provable): a value from a **structured-errored** result is reused
    as an argument to a later **side-effecting** tool call (metadata) — the agent fed data from a
    failed call into a real-world action with no fallback (the spec's ``send_itinerary`` case).
    "From" means dataflow, not overlap: the value must be one nothing else the agent observed
    supplies (an order id from the user's own request, echoed back in the error, is not from the
    failure). Another failed result is not such a source (a retry that fails again repeats the
    value, it does not confirm it), and neither is a call the agent passed the value to; but if that
    call handed the value back, it may have confirmed it (a lookup) or only echoed it (a log), so
    the use is a candidate. Retries and recoveries of the failed tool itself are how agents handle
    an error, not a misuse of it, so they are skipped. Every later call is checked, so a harmless
    logging call cannot hide the side effect after it, and a call that misuses a failure is
    reported once.
  - ``candidate`` otherwise: the same consumption into a non-side-effecting tool (could be
    legitimate error forwarding/logging), or a structured error the agent never retried before
    proceeding (judging whether a natural-language reply "acknowledged" it is not deterministic).

Without tool metadata, R2b cannot reach the hard tier — no ground truth, no hard verdict. A later
call whose arguments the trace did not record cannot be checked for reuse, so it is disclosed as not
checked rather than read as "no value reused".
"""

from __future__ import annotations

import re
from collections.abc import Callable
from functools import cache
from typing import Any, NamedTuple

from tracelint.findings import (
    SUPPRESS_NEEDS_CONTRACT,
    SUPPRESS_NOT_RECORDED,
    ConfidenceTier,
    Coverage,
    Finding,
)
from tracelint.predicates import PredicateResult
from tracelint.provenance import build_provenance
from tracelint.rules.base import Rule
from tracelint.signatures import command_text, is_interrupt, is_search_command
from tracelint.signatures import is_structured_error as _is_structured_error
from tracelint.signatures import looks_empty as _looks_empty
from tracelint.tools import ToolRegistry
from tracelint.trace import ResultStatus, ToolCall, ToolResult, Trace
from tracelint.valueutil import http_status_code
from tracelint.valueutil import significant_values as _significant_values

# Heuristic markers for an error a tool *reports* in a free-form (unknown-status) result. A tool
# reports an error at the start of a line — ``ERROR: ...``, ``fatal: ...``, ``ValueError: ...``, a
# traceback, ``bash: x: command not found`` — never in the middle of other text. Matching an error
# word anywhere read a file's own source (``raise ValueError``) as a failure: in an audit of real
# agent runs (experiments/swebench, R2a re-audit) that was every one of the heuristic's false
# positives, and every true one began a line. It only produces a *candidate* (possible FP).
_ERROR_LINE_RE = re.compile(
    r"^[ \t]*(?:\[[^\]\n]{1,24}\]:?[ \t]*)?"  # an optional log tag: "[rank0]: ", "[12:00:01] "
    r"(?:"
    r"traceback \(most recent call last\)"  # a Python traceback
    r"|(?:error|fatal|exception|failed|failure)\b"  # "ERROR: ...", "fatal: ...", "Failed to ..."
    # "ValueError: ...", "requests.ConnectionError: ...", a bare "AssertionError" — an exception
    # line has a colon or ends the line, unlike a test named "test_connection_error (...) ... ok".
    r"|[a-z_][\w.]*(?:error|exception)(?=:|[ \t]*$)"
    r"|<[\w-]*error[\w-]*>"  # "<tool_use_error>..."
    r"|[\w./-]+:[ \t]+(?:\*\*\*|error\b|fatal\b)"  # "gcc: error: ...", "make: *** ..."
    r"|[\w./-]+:[^\n]*?(?:command not found|no such file or directory|permission denied"
    r"|cannot access|is a directory|not a directory)"  # "bash: x: command not found"
    r"|http(?:/[\d.]+)?[ \t]+[45]\d\d\b"  # "HTTP/1.1 404 Not Found"
    r")",
    re.IGNORECASE | re.MULTILINE,
)


def _exception_marker(content: Any) -> str | None:
    if isinstance(content, str):
        m = _ERROR_LINE_RE.search(content)
        if m:
            return m.group(0).strip()
    return None


def _searched(call: ToolCall | None) -> bool:
    """The call ran a search command (``grep``, ``rg``, ...), for which no output means no match."""
    return call is not None and is_search_command(command_text(call))


# A conservative, TOP-LEVEL-only failure convention: a result dict whose own ``status`` field says
# the operation failed. Deliberately shallow — a nested ``jobs[].status == "failed"`` means a failed
# *item was retrieved*, not that the call failed, so we never descend. Reading this convention as a
# *fact* would be interpreting domain semantics; declaring ``failure_when`` is what makes it a fact.
# So it only ever yields a *candidate* that names the fix (convention → hint, contract → fact).
_CONVENTION_FAIL_STATES = {"error", "failed", "failure"}


def _convention_failure_marker(content: Any) -> str | None:
    if isinstance(content, dict):
        status = content.get("status")
        if isinstance(status, str) and status.strip().lower() in _CONVENTION_FAIL_STATES:
            return f"status={status!r}"
        # A status code in the body is data too: a link checker reports ``status_code: 404`` for a
        # page it checked successfully, while an API wrapper's 404 is a failure. Only a declared
        # failure_when can tell them apart, so it is a hint, never a fact.
        for key in ("http_status", "status_code"):
            code = http_status_code(content.get(key))
            if code is not None and code >= 400:
                return f"{key}={code}"
    return None


class ToolErrorEventRule(Rule):
    """R2a: a tool returned an error (structured → hard_event, heuristic → candidate)."""

    id = "R2a"
    finding_type = "tool_error_event"

    def applicable(self, trace: Trace, registry: ToolRegistry) -> str | None:
        if not trace.tool_results():
            return self.not_applicable("trace has no tool results")
        return None

    def coverage(self, trace: Trace, registry: ToolRegistry) -> Coverage | None:
        results = trace.tool_results()
        # Evaluatable = the result is structurally classifiable — a structured error, a declared
        # failure_when that resolves (MATCH/NO_MATCH, not UNKNOWN), or an explicit OK. The rest fall
        # to the heuristic (candidate) tier, i.e. R2a could not verifiably decide them.
        evaluatable = 0
        for result in results:
            call = trace.call_for(result)
            meta = registry.metadata_for(call.name) if call else None
            predicate = meta.failure_when if meta else None
            if _is_structured_error(result):
                evaluatable += 1  # a structured error is always decidable
            elif predicate is not None:
                # A declared contract counts as evaluated only if it actually resolved — a
                # declared-but-UNKNOWN predicate is *not* rescued by a status-OK fallthrough.
                if predicate.evaluate(result.content) is not PredicateResult.UNKNOWN:
                    evaluatable += 1
            elif result.status is ResultStatus.OK:
                evaluatable += 1  # explicit transport success, no declared contract to check
        return Coverage(self.id, "tool results", evaluatable, len(results))

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
            # Zero-config convention hint: the result's own top-level ``status`` says it failed, but
            # the tool declares no failure_when. Runs even on a transport-OK result (an HTTP-200
            # error envelope is the exact case), because we cannot *prove* a failure without the
            # contract — so surface it as a candidate that names the fix, never a hard event.
            if predicate is None:
                convention = _convention_failure_marker(result.content)
                if convention is not None:
                    findings.append(self._convention_candidate(result, tool, convention))
                    continue
            # Heuristics only on an unknown-status result — trust an explicit OK. Nor on a Ctrl-C
            # the agent sent: its output is the stopped command's log (often a KeyboardInterrupt).
            interrupted = call is not None and is_interrupt(command_text(call))
            if result.status is ResultStatus.OK or interrupted:
                continue
            marker = _exception_marker(result.content)
            if marker is not None:
                findings.append(self._candidate(result, tool, "exception_text", marker))
            elif _looks_empty(result.content) and not _searched(call):
                # An empty result is a candidate error — unless the call was a search, where
                # nothing printed means nothing matched.
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
            suppressed_category=SUPPRESS_NEEDS_CONTRACT,
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
            suppressed_category=SUPPRESS_NOT_RECORDED,
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

    def _convention_candidate(self, result: ToolResult, tool: str, marker: str) -> Finding:
        return Finding(
            rule=self.id,
            finding_type=self.finding_type,
            tier=ConfidenceTier.CANDIDATE,
            summary=(
                f"{tool!r} result matches a failure convention ({marker}) but declares no "
                "failure_when contract — declare one to make this a deterministic event"
            ),
            evidence={
                "step_indices": [result.index],
                "tool": tool,
                "signal": "status_convention",
                "matched": marker,
            },
            possible_false_positive=True,
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


def _failed(trace: Trace, result: ToolResult, registry: ToolRegistry) -> bool:
    """A structured error, or a result matching its tool's declared ``failure_when``."""
    call = trace.call_for(result)
    meta = registry.metadata_for(call.name) if call else None
    predicate = meta.failure_when if meta else None
    return _is_structured_error(result) or (
        predicate is not None and predicate.matches(result.content)
    )


class _Use(NamedTuple):
    """A later call that used values only a failed result supplied."""

    call: ToolCall
    values: set[str]
    # Calls that were given those values and whose results handed every one of them back: an echo
    # (a log) or a confirmation (a lookup). The trace cannot tell which, so the use is not certain.
    echoed_by: list[str]


class ErrorHandlingRule(Rule):
    """R2b: a structured error consumed by / ignored before a later action."""

    id = "R2b"
    finding_type = "error_mishandled"

    def applicable(self, trace: Trace, registry: ToolRegistry) -> str | None:
        if not trace.tool_results():
            return self.not_applicable("trace has no tool results")
        return None

    def run(self, trace: Trace, registry: ToolRegistry) -> list[Finding]:
        findings: list[Finding] = []
        calls = trace.tool_calls()
        failures = [result for result in trace.tool_results() if _failed(trace, result, registry)]
        # Where the agent saw each value, outside failed results: a retry that fails again repeats
        # the cached value, it does not confirm it.
        failed_steps = {result.index for result in failures}
        observed = build_provenance(
            [step for step in trace.steps if step.index not in failed_steps], len(trace.steps)
        )
        sources = cache(observed.sources_of)
        reported: set[int] = set()  # calls already reported as using an earlier failure's value
        unchecked: list[ToolCall] = []  # later calls whose unrecorded arguments may hold the value
        for result in failures:
            errored_call = trace.call_for(result)
            err_vals = _significant_values(result.content) | _significant_values(result.error or "")
            uses = self._uses(trace, result, errored_call, err_vals, sources, unchecked)
            if uses:
                new = [use for use in uses if use.call.index not in reported]
                if new:
                    findings.append(self._consumption(result, errored_call, new, registry))
                    reported.update(use.call.index for use in new)
                continue  # used: reported here, or with the earlier failure it repeats

            # Not consumed — was the failing tool retried afterwards? If not, it may be ignored.
            failing = errored_call.name if errored_call else None
            retried = any(c.name == failing and c.index > result.index for c in calls)
            if not retried:
                findings.append(self._unhandled(result, failing))
        disclosure = self.unknown_args_suppression(
            unchecked, "reuse of values from an earlier failed result"
        )
        if disclosure is not None:
            findings.append(disclosure)
        return findings

    def _uses(
        self,
        trace: Trace,
        result: ToolResult,
        errored_call: ToolCall | None,
        err_vals: set[str],
        sources: Callable[[str], list[int] | None],
        unchecked: list[ToolCall],
    ) -> list[_Use]:
        """Later calls that use a value only the failed ``result`` supplied, in trace order."""
        if not err_vals:
            return []
        failing = errored_call.name if errored_call else None
        fed: dict[int, ToolCall] = {}  # results of calls given the failed value -> those calls
        uses: list[_Use] = []
        for call in trace.tool_calls():
            if call.index <= result.index or call.name == failing:
                continue  # before the failure, or a retry / recovery of the failed tool
            if call.args_unavailable is not None:
                unchecked.append(call)  # a value could be in the unrecorded arguments
                continue
            # Where had the agent seen each shared value by now? A call it passed the value to does
            # not count: that only hands the value back.
            seen: dict[str, list[int]] = {}
            for value in err_vals & _significant_values(call.args):
                steps = sources(value)
                if steps is not None:  # None: too trivial to trace
                    seen[value] = [step for step in steps if step < call.index]
            from_failure = {value for value, steps in seen.items() if set(steps) <= fed.keys()}
            if not from_failure:
                continue
            echoed = all(seen[value] for value in from_failure)
            echoed_by = sorted({fed[seen[v][0]].name for v in from_failure}) if echoed else []
            uses.append(_Use(call, from_failure, echoed_by))
            downstream = trace.result_for(call)
            if downstream is not None:
                fed[downstream.index] = call
        return uses

    def _consumption(
        self,
        result: ToolResult,
        errored_call: ToolCall | None,
        uses: list[_Use],
        registry: ToolRegistry,
    ) -> Finding:
        def side_effecting(call: ToolCall) -> bool:
            meta = registry.metadata_for(call.name)
            return bool(meta and meta.side_effecting)

        def severity(use: _Use) -> int:
            if not side_effecting(use.call):
                return 0
            return 1 if use.echoed_by else 2

        use = max(uses, key=severity)  # the most severe use; the first of equals
        is_side_effecting = side_effecting(use.call)
        hard = is_side_effecting and not use.echoed_by
        errored_tool = errored_call.name if errored_call else "?"
        values = ", ".join(sorted(use.values))
        evidence: dict[str, Any] = {
            "step_indices": [result.index, use.call.index],
            "errored_tool": errored_tool,
            "consumer": use.call.name,
            "consumed_values": sorted(use.values),
            "side_effecting": is_side_effecting,
        }
        if use.echoed_by:
            evidence["echoed_by"] = use.echoed_by
        also = [other.call.name for other in uses if other is not use]
        if also:
            evidence["also_used_by"] = also
        # Every side effect the failure reached, not just the one reported: a CI baseline must
        # notice when the same failure starts feeding a new action.
        reached = sorted({u.call.name for u in uses if side_effecting(u.call)})
        if reached:
            evidence["side_effecting_uses"] = reached
        summary = (
            f"value(s) from the errored {errored_tool!r} result ({values}) reused as arguments "
            f"to {use.call.name!r}"
        )
        if hard:
            summary += " (a side-effecting action, no fallback)"
        elif is_side_effecting:
            echoes = ", ".join(repr(name) for name in use.echoed_by)
            summary += (
                f" (a side-effecting action, but {echoes} returned the value when given it, which "
                "may confirm it)"
            )
        return Finding(
            rule=self.id,
            finding_type=self.finding_type,
            tier=ConfidenceTier.HARD_DEFECT if hard else ConfidenceTier.CANDIDATE,
            summary=summary,
            evidence=evidence,
            possible_false_positive=not hard,
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
