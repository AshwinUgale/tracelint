"""R3 — Hallucinated argument (spec §II.5, R3; deep-design Trap 2).

For each argument value in a tool call, R3 asks the provenance graph (``provenance.py``) whether
the value is derivable from what the agent actually observed. A value that is *not* derivable is
suspicious — but not automatically a defect, because a UUID or idempotency key is a *legitimately
generated* value. So the confidence is gated (Trap 2, the "trusted-doc problem reborn"):

- The field's schema declares ``x-value-origin: "generated"``  → **skip** (legitimate).
- The field's schema declares ``x-value-origin: "provided"`` and the value is underivable →
  ``hard_defect`` (high confidence: a field that must come from context is absent and underivable).
- No annotation and the value is underivable → ``candidate`` with ``possible_false_positive``
  (could be a legitimate generated value or an unrecognized transform — shown for review).

Fields the model legitimately *chooses* rather than *derives* are skipped: ``enum`` / ``const``
fields, and booleans. Nested object/array arguments are out of scope for the MVP.

**Honest consequence (disclosed in the README):** most users will not annotate schemas, so
out-of-box hallucination detection is candidate-only; the high-confidence tier is opt-in effort.
"""

from __future__ import annotations

from typing import Any

from tracelint.findings import ConfidenceTier, Finding
from tracelint.provenance import build_provenance
from tracelint.rules.base import Rule
from tracelint.tools import ToolRegistry
from tracelint.trace import ToolCall, Trace


class HallucinatedArgRule(Rule):
    """R3: tool-call arguments should be derivable from the agent's observed provenance."""

    id = "R3"
    finding_type = "hallucinated_arg"

    def applicable(self, trace: Trace, registry: ToolRegistry) -> str | None:
        if not trace.tool_calls():
            return "trace has no tool calls to check"
        return None

    def run(self, trace: Trace, registry: ToolRegistry) -> list[Finding]:
        findings: list[Finding] = []
        for call in trace.tool_calls():
            spec = registry.get(call.name)
            props = (spec.schema or {}).get("properties", {}) if spec and spec.schema else {}
            origins = spec.value_origins if spec else {}
            graph = build_provenance(trace.steps, call.index)

            for field_name, value in call.args.items():
                if not self._checkable(value, props.get(field_name, {}), origins.get(field_name)):
                    continue
                if graph.derive(value).derivable:
                    continue
                findings.append(self._finding(call, field_name, value, origins.get(field_name)))
        return findings

    def _checkable(self, value: Any, subschema: dict[str, Any], origin: str | None) -> bool:
        if origin == "generated":
            return False  # legitimately generated (UUID, idempotency key, ...)
        if isinstance(value, bool) or value is None:
            return False
        if isinstance(value, (dict, list)):
            return False  # nested arguments are out of MVP scope
        if isinstance(subschema, dict) and ("enum" in subschema or "const" in subschema):
            return False  # a closed choice the model selects, not a value it derives
        return True

    def _finding(self, call: ToolCall, field_name: str, value: Any, origin: str | None) -> Finding:
        high_confidence = origin == "provided"
        note = (
            "schema declares this field 'provided', so an absent, underivable value is a defect"
            if high_confidence
            else "no schema origin declared — could be a legitimate generated value or an "
            "unrecognized transform"
        )
        return Finding(
            rule=self.id,
            finding_type=self.finding_type,
            tier=ConfidenceTier.HARD_DEFECT if high_confidence else ConfidenceTier.CANDIDATE,
            summary=(
                f"argument {field_name!r}={value!r} to {call.name!r} is not derivable from "
                f"anything the agent observed"
            ),
            evidence={
                "step_indices": [call.index],
                "tool": call.name,
                "field": field_name,
                "value": value,
                "value_origin": origin,
                "note": note,
            },
            possible_false_positive=not high_confidence,
        )
