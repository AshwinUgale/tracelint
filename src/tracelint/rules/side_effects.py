"""R8 — duplicate side effect (ROADMAP side-effect story).

R5 flags a *redundant* call — a repeated read whose result was byte-identical, i.e. wasted work.
R8 is its dangerous sibling: the same tool called again with **equivalent arguments** when the tool
is a **declared, non-idempotent side effect** and the first call did **not** fail — so the first
almost certainly mutated the world and the repeat risks doing it twice (the double-charge). Unlike
R4 (a loop needs three), a *single* repeat already duplicates the effect, so R8 fires on the second.

Tiered by the evidence about the first call — never asserting intent, since charging twice can be
deliberate, so this reports an *event*, not a defect:
  - ``hard_event`` when the first result is a **known success** (an explicit OK, or a declared
    ``failure_when`` that resolved to *not a failure*): the first definitely ran, so an equivalent
    repeat is a structurally-provable duplicate effect.
  - ``candidate`` when the first result is **unknown**: it may have failed silently, which would
    make the repeat a legitimate retry — so this is disclosed with its evidence, not asserted.
A first call that *did* fail (a structured error or a declared failure) makes the repeat a
legitimate retry and is never flagged.

Equivalence is exact normalized arguments: two charges for *different* orders (different args, or a
distinct idempotency key) are not flagged, while a repeat with the *same* arguments — including a
reused idempotency key — is. Side-effect and idempotency status are read from declared metadata,
never guessed from a name; an undeclared tool is not assumed to be a side effect, so R8 abstains on
it rather than flag or wave it through.
"""

from __future__ import annotations

from tracelint.findings import ConfidenceTier, Finding
from tracelint.predicates import PredicateResult
from tracelint.rules.base import Rule
from tracelint.signatures import is_structured_error as _is_structured_error
from tracelint.signatures import normalize_args
from tracelint.tools import ToolMetadata, ToolRegistry
from tracelint.trace import ResultStatus, ToolCall, ToolResult, Trace


class DuplicateSideEffectRule(Rule):
    """R8: an equivalent, non-idempotent side-effecting call repeated without the first failing."""

    id = "R8"
    finding_type = "duplicate_side_effect"

    def applicable(self, trace: Trace, registry: ToolRegistry) -> str | None:
        if len(trace.tool_calls()) < 2:
            return "fewer than 2 tool calls; no duplicate possible"
        return None

    def run(self, trace: Trace, registry: ToolRegistry) -> list[Finding]:
        findings: list[Finding] = []
        seen: dict[tuple[str, str], ToolCall] = {}  # (tool, normalized args) -> anchor call
        for call in trace.tool_calls():
            meta = registry.metadata_for(call.name)
            if not (meta and meta.side_effecting and not meta.idempotent):
                continue
            key = (call.name, normalize_args(call.args))
            anchor = seen.get(key)
            if anchor is None:
                seen[key] = call
                continue
            outcome = self._first_outcome(trace.result_for(anchor), meta)
            if outcome == "failure":
                seen[key] = call  # the first failed → this repeat is a legitimate retry
                continue
            findings.append(self._finding(anchor, call, outcome))
            seen[key] = call  # chain to the most recent equivalent call
        return findings

    def _first_outcome(self, result: ToolResult | None, meta: ToolMetadata) -> str:
        """``'failure'`` | ``'success'`` | ``'unknown'`` for the earlier call's result."""
        if result is None:
            return "unknown"
        if _is_structured_error(result):
            return "failure"
        predicate = meta.failure_when
        if predicate is not None:
            verdict = predicate.evaluate(result.content)
            if verdict is PredicateResult.MATCH:
                return "failure"
            if verdict is PredicateResult.NO_MATCH:
                return "success"
            return "unknown"  # a declared contract that could not be evaluated
        return "success" if result.status is ResultStatus.OK else "unknown"

    def _finding(self, first: ToolCall, again: ToolCall, outcome: str) -> Finding:
        known = outcome == "success"
        why = (
            "the first call succeeded, so this repeats the effect"
            if known
            else "the first call's outcome is unknown (it may have failed, making this a retry)"
        )
        return Finding(
            rule=self.id,
            finding_type=self.finding_type,
            tier=ConfidenceTier.HARD_EVENT if known else ConfidenceTier.CANDIDATE,
            summary=(
                f"{again.name!r} repeats an equivalent non-idempotent side-effecting call "
                f"(same arguments) — {why}"
            ),
            evidence={
                "step_indices": [first.index, again.index],
                "tool": again.name,
                "first_outcome": outcome,
            },
            possible_false_positive=not known,
        )
