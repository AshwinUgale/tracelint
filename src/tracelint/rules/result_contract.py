"""R10 — Result contract: a tool's output drifting from its declared schema.

A tool may declare an output JSON Schema (``output_schema``, or MCP's ``outputSchema``). R10 replays
each recorded **successful** result against it — the mirror of R1, which replays the arguments the
model emitted. A result that violates the schema is *tool output drift*: the tool returned something
off-contract (an API version bump, a stale schema). It is a ``hard_event`` — a certain structural
fact, but the tool's output rather than the agent's own defect (unlike R1's ``hard_defect``) — so it
is shown yet does not fail CI unless the operator opts in with ``--fail-on hard_event``.

Opt-in, like the other contract-metadata rules (R8/R9): a tool with no ``output_schema`` is silent —
no finding, no suppression, no coverage line — so declaring none costs nothing. Failed results are
R2's domain and are skipped (a declared failure isn't the success shape it describes); a success
whose content wasn't recorded is disclosed as not-checked, never validated against ``{}``.
"""

from __future__ import annotations

from typing import Any

from jsonschema.exceptions import SchemaError
from jsonschema.validators import validator_for

from tracelint.findings import (
    SUPPRESS_NEEDS_CONTRACT,
    SUPPRESS_NOT_RECORDED,
    ConfidenceTier,
    Coverage,
    Finding,
)
from tracelint.rules.base import Rule
from tracelint.rules.error_handling import _failed
from tracelint.rules.schema_violation import _pointer
from tracelint.tools import ToolRegistry
from tracelint.trace import ToolResult, Trace


class ResultContractRule(Rule):
    """R10: a recorded result must satisfy its tool's declared output JSON Schema."""

    id = "R10"
    finding_type = "result_contract_violation"

    def _tool_schema(
        self, trace: Trace, registry: ToolRegistry, result: ToolResult
    ) -> tuple[str | None, dict[str, Any] | None]:
        call = trace.call_for(result)
        name = call.name if call else None
        schema = registry.output_schema_for(name) if name else None
        return name, schema

    def applicable(self, trace: Trace, registry: ToolRegistry) -> None:
        # Opt-in: never suppress for a missing output_schema (most tools declare none). run() and
        # coverage() are dormant unless a tool that returned a result declares one.
        return None

    def coverage(self, trace: Trace, registry: ToolRegistry) -> Coverage | None:
        relevant = [
            r for r in trace.tool_results() if self._tool_schema(trace, registry, r)[1] is not None
        ]
        if not relevant:
            return None  # no tool declared an output_schema: the rule is dormant
        evaluatable = sum(
            1 for r in relevant if not _failed(trace, r, registry) and r.content is not None
        )
        return Coverage(self.id, "tool results", evaluatable, len(relevant))

    def run(self, trace: Trace, registry: ToolRegistry) -> list[Finding]:
        findings: list[Finding] = []
        for result in trace.tool_results():
            name, schema = self._tool_schema(trace, registry, result)
            if schema is None:
                continue  # opt-in: this tool declares no output contract
            if _failed(trace, result, registry):
                continue  # a failure isn't the success shape output_schema describes (R2's domain)
            if result.content is None:
                findings.append(
                    self._suppress(
                        result, name, "result content not recorded", SUPPRESS_NOT_RECORDED
                    )
                )
                continue
            finding = self._validate(result, name, schema)
            if finding is not None:
                findings.append(finding)
        return findings

    def _validate(
        self, result: ToolResult, name: str | None, schema: dict[str, Any]
    ) -> Finding | None:
        validator_cls = validator_for(schema)
        try:
            validator_cls.check_schema(schema)
        except SchemaError as exc:
            return self._suppress(
                result,
                name,
                f"tool {name!r} has an invalid output_schema: {exc.message}",
                SUPPRESS_NEEDS_CONTRACT,
            )
        validator = validator_cls(schema)
        errors = [
            {"path": _pointer(e.absolute_path), "keyword": e.validator, "message": e.message}
            for e in validator.iter_errors(result.content)
        ]
        if not errors:
            return None
        errors.sort(key=lambda e: (e["path"], str(e["keyword"])))
        n = len(errors)
        return Finding(
            rule=self.id,
            finding_type=self.finding_type,
            tier=ConfidenceTier.HARD_EVENT,
            summary=(
                f"{name!r} result violates its output_schema "
                f"({n} error{'s' if n != 1 else ''}: "
                f"{', '.join(sorted({str(e['keyword']) for e in errors}))})"
            ),
            evidence={
                "step_indices": [result.index],
                "tool": name,
                "call_id": result.call_id,
                "errors": errors,
            },
        )

    def _suppress(
        self, result: ToolResult, name: str | None, reason: str, category: str
    ) -> Finding:
        return Finding(
            rule=self.id,
            finding_type=self.finding_type,
            tier=ConfidenceTier.CANDIDATE,
            summary=f"rule {self.id} suppressed for {name!r}: {reason}",
            evidence={"step_indices": [result.index], "tool": name, "call_id": result.call_id},
            suppressed_reason=reason,
            suppressed_category=category,
        )
