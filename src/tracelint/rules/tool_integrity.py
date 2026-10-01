"""R6 malformed arguments + R7 unknown tool — two more judge-free structural rules.

Both are decidable from the trace plus declared ground truth, in the same spirit as R1:

- **R6 (malformed arguments)** — a tool call's emitted ``arguments`` were not valid JSON at all.
  Tool-calling APIs require ``arguments`` to be a JSON *object*; when the model emits a broken
  string the adapter keeps it in :attr:`ToolCall.raw_text` with empty ``args`` (it could not be
  parsed). R6 confirms the string really is invalid JSON and reports a ``hard_defect`` — it is a
  provable syntactic fault that R1 cannot see (R1 needs *parseable* args to validate a schema).
  Only text the *model* emitted is proof. A tool's recorded input is not the model's output: a bare
  value or a redacted mask is marked unknown by the adapters, and broken JSON there is reported as a
  ``candidate`` — the model may have emitted it, or an exporter may have truncated a long input.
- **R7 (unknown tool)** — a tool was called whose name is not in the declared registry (a likely
  *hallucinated tool name*). This is only as strong as the registry's completeness, so it is a
  ``candidate`` with the false-positive caveat, not a hard defect: a partial registry legitimately
  omits tools. Suppressed entirely when no registry is supplied (no ground truth to compare to).
"""

from __future__ import annotations

import json

from tracelint.findings import ConfidenceTier, Finding
from tracelint.rules.base import Rule
from tracelint.tools import ToolRegistry
from tracelint.trace import ToolCall, Trace

# Framework-owned control tools that appear in agent traces but are never part of a user's declared
# toolset — e.g. smolagents' terminal ``final_answer``. The framework emits them; the model does not
# pick them from the app's tools, so R7 must not flag them as possible hallucinated tool names (that
# is onboarding noise the user cannot fix without declaring a tool they never wrote). This is a
# starter allowlist by name; the general form is a ``framework_internal`` tag carried on the
# canonical ToolCall at normalization time (roadmap).
FRAMEWORK_INTERNAL_TOOLS = frozenset({"final_answer"})


def _is_invalid_json_object(raw: str) -> bool:
    """True if ``raw`` is not a valid JSON *object* (parse fails, or parses to a non-object)."""
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return True
    return not isinstance(parsed, dict)


class MalformedArgumentsRule(Rule):
    """R6: a tool call's arguments must be valid JSON (a syntactic, provable check)."""

    id = "R6"
    finding_type = "malformed_arguments"

    def applicable(self, trace: Trace, registry: ToolRegistry) -> str | None:
        if not trace.tool_calls():
            return "trace has no tool calls to check"
        return None

    def run(self, trace: Trace, registry: ToolRegistry) -> list[Finding]:
        findings: list[Finding] = []
        for call in trace.tool_calls():
            raw = call.raw_text
            # The adapter only leaves raw_text with empty args when parsing failed; confirm it.
            if raw and not call.args and _is_invalid_json_object(raw):
                findings.append(self._finding(call, raw))
        return findings

    def _finding(self, call: ToolCall, raw: str) -> Finding:
        # Unknown arguments with raw text: only the tool's recorded input was broken JSON, which an
        # exporter's truncation also produces, so it is shown for review rather than failing CI.
        emitted = call.args_unavailable is None
        summary = (
            f"{call.name!r} call has malformed arguments — the emitted string is not valid JSON"
            if emitted
            else f"{call.name!r} call's recorded input is not valid JSON — malformed arguments, or "
            "a record truncated by the exporter (the model's own call is not in the trace)"
        )
        return Finding(
            rule=self.id,
            finding_type=self.finding_type,
            tier=ConfidenceTier.HARD_DEFECT if emitted else ConfidenceTier.CANDIDATE,
            summary=summary,
            evidence={
                "step_indices": [call.index],
                "tool": call.name,
                "call_id": call.call_id,
                "raw_arguments": raw,
            },
            possible_false_positive=not emitted,
        )


class UnknownToolRule(Rule):
    """R7: a called tool should be in the declared toolset (else a possible hallucinated tool)."""

    id = "R7"
    finding_type = "unknown_tool"

    def applicable(self, trace: Trace, registry: ToolRegistry) -> str | None:
        if not trace.tool_calls():
            return "trace has no tool calls to check"
        if len(registry) == 0:
            return "no tool registry supplied — cannot know which tools were declared"
        return None

    def run(self, trace: Trace, registry: ToolRegistry) -> list[Finding]:
        declared = sorted(registry.names())
        findings: list[Finding] = []
        for call in trace.tool_calls():
            if call.name in FRAMEWORK_INTERNAL_TOOLS:
                continue  # framework-owned control tool (e.g. final_answer) — not user-declared
            if registry.get(call.name) is None:
                findings.append(
                    Finding(
                        rule=self.id,
                        finding_type=self.finding_type,
                        tier=ConfidenceTier.CANDIDATE,
                        summary=(
                            f"tool {call.name!r} was called but is not in the declared toolset "
                            "(possible hallucinated tool name)"
                        ),
                        evidence={
                            "step_indices": [call.index],
                            "tool": call.name,
                            "call_id": call.call_id,
                            "declared_tools": declared,
                        },
                        possible_false_positive=True,
                    )
                )
        return findings
