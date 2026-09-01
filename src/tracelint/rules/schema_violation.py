"""R1 — Schema violation (spec §II.5, R1; learning-doc 02 §3).

A tool-calling API requires every tool to declare a JSON Schema for its arguments. A trace
records what the model *actually* emitted (``ToolCall.args``). R1 replays each recorded call
against its declared schema, after the run, with a standard JSON Schema validator — a purely
**static, deterministic** check that needs only the schema and the recorded arguments, never
re-invokes the model or the tool, and has near-zero false positives when the schema is accurate.
A violation is a ``hard_defect`` (the tier reserved for structurally-provable defects), so it
drives the non-zero CI exit.

Fail-closed granularity (Trap 1): the rule is suppressed at the *trace* level only when no called
tool has a schema at all (nothing to check). When *some* tools have schemas, R1 runs and emits a
**per-call suppression** for each call whose tool is unknown or whose schema is itself invalid —
so a partial registry checks what it can and discloses what it cannot, never faking a clean pass.
"""

from __future__ import annotations

from typing import Any

from jsonschema.exceptions import SchemaError
from jsonschema.validators import validator_for

from tracelint.findings import ConfidenceTier, Coverage, Finding
from tracelint.rules.base import Rule
from tracelint.tools import ToolRegistry
from tracelint.trace import ToolCall, Trace


def _pointer(path: Any) -> str:
    """Render a jsonschema error's ``absolute_path`` deque as a JSON Pointer (``/order_id``)."""
    parts = list(path)
    return "/" + "/".join(str(p) for p in parts) if parts else "(root)"


class SchemaViolationRule(Rule):
    """R1: recorded tool-call arguments must satisfy the tool's declared JSON Schema."""

    id = "R1"
    finding_type = "schema_violation"

    def applicable(self, trace: Trace, registry: ToolRegistry) -> str | None:
        calls = trace.tool_calls()
        if not calls:
            return "trace has no tool calls to validate"
        if not any(registry.schema_for(c.name) is not None for c in calls):
            return "no tool schema available for any called tool"
        return None

    def coverage(self, trace: Trace, registry: ToolRegistry) -> Coverage | None:
        calls = trace.tool_calls()
        # Evaluatable = a schema is declared for the called tool (a declared-but-invalid schema is
        # reported separately as a per-call suppression, so it doesn't count as verified here).
        evaluatable = sum(1 for c in calls if registry.schema_for(c.name) is not None)
        return Coverage(self.id, "tool calls", evaluatable, len(calls))

    def run(self, trace: Trace, registry: ToolRegistry) -> list[Finding]:
        findings: list[Finding] = []
        for call in trace.tool_calls():
            schema = registry.schema_for(call.name)
            if schema is None:
                findings.append(self._suppress(call, f"no schema for tool {call.name!r}"))
                continue
            finding = self._validate_call(call, schema)
            if finding is not None:
                findings.append(finding)
        return findings

    def _validate_call(self, call: ToolCall, schema: dict[str, Any]) -> Finding | None:
        validator_cls = validator_for(schema)
        try:
            validator_cls.check_schema(schema)
        except SchemaError as exc:
            return self._suppress(
                call, f"tool {call.name!r} has an invalid JSON Schema: {exc.message}"
            )

        validator = validator_cls(schema)
        errors = [
            {"path": _pointer(e.absolute_path), "keyword": e.validator, "message": e.message}
            for e in validator.iter_errors(call.args)
        ]
        if not errors:
            return None
        # Deterministic ordering — jsonschema does not guarantee error order.
        errors.sort(key=lambda e: (e["path"], str(e["keyword"])))
        n = len(errors)
        return Finding(
            rule=self.id,
            finding_type=self.finding_type,
            tier=ConfidenceTier.HARD_DEFECT,
            summary=(
                f"{call.name!r} call violates its schema "
                f"({n} error{'s' if n != 1 else ''}: "
                f"{', '.join(sorted({str(e['keyword']) for e in errors}))})"
            ),
            evidence={
                "step_indices": [call.index],
                "tool": call.name,
                "call_id": call.call_id,
                "errors": errors,
            },
        )

    def _suppress(self, call: ToolCall, reason: str) -> Finding:
        return Finding(
            rule=self.id,
            finding_type=self.finding_type,
            tier=ConfidenceTier.CANDIDATE,
            summary=f"rule {self.id} suppressed for call {call.call_id}: {reason}",
            evidence={"step_indices": [call.index], "tool": call.name, "call_id": call.call_id},
            suppressed_reason=reason,
        )
