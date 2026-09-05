"""Bootstrap a starter ``tools.json`` from a trace — the engine behind ``tracelint init``.

The #1 onboarding friction is writing a ``tools.json`` by hand. But a trace already proves *which*
tools were called, and OpenInference/OTel traces often carry each tool's argument JSON Schema
(``tool.parameters`` / ``llm.tools.*.tool.json_schema``, on :attr:`ToolCall.schema`). When the trace
doesn't declare a schema, one is **inferred from the argument values actually observed**. So the
only thing left for a human is the **behavior** tracelint can't see from a trace —
``side_effecting`` / ``idempotent`` / ``failure_when``.

``discover_contract`` turns a trace into a valid ``tools.json``: a schema per tool (declared where
the trace carried it, else inferred and marked for review), the behavior fields as explicit ``null``
placeholders (which :class:`~tracelint.tools.ToolRegistry` reads as the conservative default, so the
draft is a valid, round-trippable contract), and a per-tool ``_todo`` naming exactly what to fill in
(JSON has no comments, so the TODOs live in the data — and are ignored on load). Framework-internal
control tools (e.g. ``final_answer``) are skipped, matching R7.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from tracelint.rules.tool_integrity import FRAMEWORK_INTERNAL_TOOLS
from tracelint.trace import Trace

_BEHAVIOR_PLACEHOLDER: dict[str, Any] = {
    "side_effecting": None,
    "idempotent": None,
    "failure_when": None,
}

_TODO_BEHAVIOR = [
    "set metadata.side_effecting: true if this call changes state (charge / send / write / delete)",
    "set metadata.idempotent: true if repeating the identical call is harmless",
    "set metadata.failure_when if a success response can still carry a failure "
    '(e.g. {"status": "declined"} at HTTP 200) — a JSON pointer + match',
]

_INFERRED_MARK = "inferred by `tracelint init` from observed calls — verify types, add required[]"


def _json_type(value: Any) -> str | None:
    """The JSON Schema ``type`` for an observed value (``None`` when unknown / null)."""
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return None


def _infer_schema(observed_args: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Synthesize an object schema from the argument values seen across a tool's calls.

    Conservative on purpose: it records each observed argument and its type (dropping the type
    constraint if a key appears with different types across calls), but never guesses ``required`` —
    a starter schema should not manufacture a false R1 on the next call.
    """
    props: dict[str, Any] = {}
    for args in observed_args:
        for key, value in args.items():
            t = _json_type(value)
            if key not in props:
                props[key] = {"type": t} if t else {}
            elif t is not None and props[key].get("type") not in (None, t):
                props[key].pop("type", None)  # inconsistent across calls → leave it unconstrained
    if not props:
        return None
    return {"type": "object", "properties": props, "$comment": _INFERRED_MARK}


@dataclass
class ContractDraft:
    """A starter contract discovered from a trace, plus what still needs human review."""

    tools: dict[str, dict[str, Any]] = field(default_factory=dict)
    with_schema: list[str] = field(default_factory=list)
    inferred_schema: list[str] = field(default_factory=list)
    no_schema: list[str] = field(default_factory=list)
    skipped_internal: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "_comment": (
                "Starter contract from `tracelint init`. Per tool, do the _todo items, then delete "
                "the _todo keys and run `tracelint check <trace> --tools tools.json`."
            ),
            "tools": self.tools,
        }

    def summary(self) -> str:
        """A human summary — the review TODOs the JSON also carries inline."""
        n = len(self.tools)
        lines = [f"tracelint init: {n} tool(s) called in the trace."]
        if self.with_schema:
            lines.append(f"  schema from the trace for {len(self.with_schema)}: "
                         + ", ".join(self.with_schema))
        if self.inferred_schema:
            lines.append(f"  schema INFERRED (verify) for {len(self.inferred_schema)}: "
                         + ", ".join(self.inferred_schema))
        if self.no_schema:
            lines.append("  no schema (no args observed) — add one for R1: "
                         + ", ".join(self.no_schema))
        if self.skipped_internal:
            lines.append(
                "  skipped framework-internal tool(s): " + ", ".join(self.skipped_internal)
            )
        if n:
            lines.append(
                "  REVIEW behavior for every tool (the trace can't prove it): set "
                "side_effecting / idempotent / failure_when — see each tool's _todo."
            )
        return "\n".join(lines)


def discover_contract(traces: Iterable[Trace]) -> ContractDraft:
    """Build a :class:`ContractDraft` from the tool calls across one or more traces."""
    schemas: dict[str, dict[str, Any]] = {}
    observed: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = []
    skipped: list[str] = []

    for trace in traces:
        for call in trace.tool_calls():
            name = call.name
            if not name:
                continue
            if name in FRAMEWORK_INTERNAL_TOOLS:
                if name not in skipped:
                    skipped.append(name)
                continue
            if name not in observed:
                order.append(name)
                observed[name] = []
            observed[name].append(call.args or {})
            if call.schema and name not in schemas:
                schemas[name] = call.schema

    draft = ContractDraft(skipped_internal=skipped)
    for name in order:
        declared = schemas.get(name)
        if declared is not None:
            schema: dict[str, Any] | None = declared
            draft.with_schema.append(name)
            schema_todo = None
        else:
            schema = _infer_schema(observed[name])
            if schema is not None:
                draft.inferred_schema.append(name)
                schema_todo = "verify the inferred argument schema (guessed from observed calls)"
            else:
                draft.no_schema.append(name)
                schema_todo = "add the argument JSON Schema (none in the trace, no args observed)"
        todo = ([schema_todo] if schema_todo else []) + _TODO_BEHAVIOR
        draft.tools[name] = {
            "schema": schema,  # object schema, or null when nothing could be discovered/inferred
            "metadata": dict(_BEHAVIOR_PLACEHOLDER),
            "_todo": todo,
        }
    return draft
