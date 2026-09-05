"""Bootstrap a starter ``tools.json`` from a trace — the engine behind ``tracelint init``.

The #1 onboarding friction is writing a ``tools.json`` by hand. But a trace already proves *which*
tools were called, and OpenInference/OTel traces often carry each tool's argument JSON Schema
(``tool.parameters`` / ``llm.tools.*.tool.json_schema``, on :attr:`ToolCall.schema`). So the only
thing a user must supply is the **behavior** tracelint cannot infer from a trace —
``side_effecting`` / ``idempotent`` / ``failure_when``.

``discover_contract`` turns a trace into a valid ``tools.json`` with schemas filled in where the
trace carried them and the behavior fields left as explicit ``null`` placeholders (which
:class:`~tracelint.tools.ToolRegistry` reads as the conservative default) — so onboarding becomes
"review a few unknowns," not "learn the contract format". Framework-internal control tools (e.g.
``final_answer``) are skipped, matching R7.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from tracelint.rules.tool_integrity import FRAMEWORK_INTERNAL_TOOLS
from tracelint.trace import Trace

# Behavior tracelint cannot decide from a trace — emitted as null placeholders for the user to fill.
# null round-trips through ToolRegistry as the conservative default (not side-effecting, not
# idempotent, no failure predicate), so the draft is always a valid contract.
_BEHAVIOR_PLACEHOLDER: dict[str, Any] = {
    "side_effecting": None,
    "idempotent": None,
    "failure_when": None,
}


@dataclass
class ContractDraft:
    """A starter contract discovered from a trace, plus what still needs human review."""

    tools: dict[str, dict[str, Any]] = field(default_factory=dict)
    with_schema: list[str] = field(default_factory=list)
    needs_schema: list[str] = field(default_factory=list)
    skipped_internal: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"tools": self.tools}

    def summary(self) -> str:
        """A human summary — the TODOs the JSON itself cannot carry (JSON has no comments)."""
        n = len(self.tools)
        lines = [f"tracelint init: {n} tool(s) called in the trace."]
        if self.with_schema:
            lines.append(f"  schema discovered for {len(self.with_schema)}: "
                         + ", ".join(self.with_schema))
        if self.needs_schema:
            lines.append("  no schema in the trace — add one for R1: "
                         + ", ".join(self.needs_schema))
        if self.skipped_internal:
            lines.append(
                "  skipped framework-internal tool(s): " + ", ".join(self.skipped_internal)
            )
        if n:
            lines.append(
                "  REVIEW behavior for every tool (the trace can't prove it) — set "
                "side_effecting / idempotent / failure_when: " + ", ".join(self.tools)
            )
        return "\n".join(lines)


def discover_contract(traces: Iterable[Trace]) -> ContractDraft:
    """Build a :class:`ContractDraft` from the tool calls across one or more traces."""
    draft = ContractDraft()
    for trace in traces:
        for call in trace.tool_calls():
            name = call.name
            if not name:
                continue
            if name in FRAMEWORK_INTERNAL_TOOLS:
                if name not in draft.skipped_internal:
                    draft.skipped_internal.append(name)
                continue
            if name in draft.tools:
                # A later call may carry the schema an earlier one lacked — backfill it.
                if draft.tools[name].get("schema") is None and call.schema:
                    draft.tools[name]["schema"] = call.schema
                    draft.needs_schema.remove(name)
                    draft.with_schema.append(name)
                continue
            draft.tools[name] = {
                "schema": call.schema,  # a JSON Schema object, or null when the trace lacked it
                "metadata": dict(_BEHAVIOR_PLACEHOLDER),
            }
            (draft.with_schema if call.schema else draft.needs_schema).append(name)
    return draft
