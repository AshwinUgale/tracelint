"""R12 — Unresolved side effect: a side-effecting action whose outcome the trace never resolved.

R2 catches *known* failures — a tool returned an error and the agent proceeded (R2a/R2b). R12
catches the case the ROADMAP calls "the agent never found out": a side-effecting call whose success
was never established in the trace. Two shapes, judged per side-effecting tool on its **last** call
(so a later successful call recovers an earlier failure, and repeated failures flag once):

1. **No recorded outcome** — the call has no result at all; the write may or may not have happened.
2. **Failed, unrecovered** — the last call of the tool failed (a structured error, or a declared
   ``failure_when``) and nothing after it succeeded.

Always **candidate**, fail-closed (the ROADMAP's explicit tier call): a missing result could be a
real hang *or* a truncated capture, so R12 discloses a signal for review and never asserts a defect.
When the call sits at the very end of the trace, truncation is plausible, so it is additionally
marked a possible false positive; when the run clearly continued past it (later calls or results
were captured), that caution is dropped. Independent read-back confirmation (a declared
``confirmed_by``) is the deeper, deferred half of this idea; R12's core needs no new contract key.
"""

from __future__ import annotations

from typing import Any

from tracelint.findings import ConfidenceTier, Finding
from tracelint.rules.base import Rule
from tracelint.rules.error_handling import _failed
from tracelint.tools import ToolRegistry
from tracelint.trace import ToolCall, ToolResult, Trace


class UnresolvedSideEffectRule(Rule):
    """R12: a side-effecting tool whose last call has no recorded outcome, or failed unrecovered."""

    id = "R12"
    finding_type = "unresolved_side_effect"

    def applicable(self, trace: Trace, registry: ToolRegistry) -> None:
        # Opt-in: only side-effecting tools (declared in tools.json) are considered; see run().
        return None

    def run(self, trace: Trace, registry: ToolRegistry) -> list[Finding]:
        last: dict[str, ToolCall] = {}
        for call in trace.tool_calls():
            meta = registry.metadata_for(call.name)
            if meta and meta.side_effecting:
                last[call.name] = call  # ends on the tool's last side-effecting call

        findings: list[Finding] = []
        for call in last.values():
            result = trace.result_for(call)
            continued = any(
                s.index > call.index for s in trace.steps if isinstance(s, (ToolCall, ToolResult))
            )
            if result is None:
                findings.append(
                    self._finding(
                        call,
                        "no recorded outcome — the run never saw whether it succeeded",
                        possible_fp=not continued,
                    )
                )
            elif _failed(trace, result, registry):
                findings.append(
                    self._finding(
                        call,
                        "its last call failed and nothing after it succeeded",
                        possible_fp=not continued,
                        steps=[call.index, result.index],
                    )
                )
        return findings

    def _finding(
        self, call: ToolCall, reason: str, *, possible_fp: bool, steps: list[int] | None = None
    ) -> Finding:
        evidence: dict[str, Any] = {
            "step_indices": steps or [call.index],
            "tool": call.name,
            "call_id": call.call_id,
        }
        return Finding(
            rule=self.id,
            finding_type=self.finding_type,
            tier=ConfidenceTier.CANDIDATE,
            summary=f"{call.name!r} side effect is unresolved: {reason}",
            evidence=evidence,
            possible_false_positive=possible_fp,
        )
