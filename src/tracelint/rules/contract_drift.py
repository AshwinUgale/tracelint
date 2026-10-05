"""R11 — Contract drift: the run-time tool schema vs the committed tools.json.

Some traces carry each tool's schema inline (OTel / OpenInference tool-definition spans, an OpenAI
``tools`` block — on :attr:`ToolCall.schema`). That is the schema the agent *actually* ran against.
The committed ``tools.json`` is the schema the operator declared. When they diverge, the committed
contract has gone stale (or the tool changed under it): R1 is then validating calls against a schema
that no longer matches the tool.

R11 compares the two **structurally** — property names, each property's ``type``, ``required``, and
the top-level ``type`` — ignoring descriptions, titles and order so a cosmetic edit never flags. A
real difference is *contract drift*: a ``hard_event`` (a certain fact, but about the contract rather
than the agent's own run), shown in the report yet not failing CI unless the operator opts in with
``--fail-on hard_event``.

Opt-in and quiet: it fires only where a tool carries *both* an inline schema and a committed one,
and both declare properties — so a native trace, a registry without inline-schema traces, or a bare
``{"type": "object"}`` placeholder all stay silent.
"""

from __future__ import annotations

from typing import Any

from tracelint.findings import ConfidenceTier, Finding
from tracelint.rules.base import Rule
from tracelint.tools import ToolRegistry
from tracelint.trace import Trace


def _props(schema: dict[str, Any] | None) -> dict[str, Any]:
    props = (schema or {}).get("properties")
    return props if isinstance(props, dict) else {}


def _prop_type(sub: Any) -> Any:
    return sub.get("type") if isinstance(sub, dict) else None


def _required(schema: dict[str, Any] | None) -> set[str]:
    req = (schema or {}).get("required")
    return set(req) if isinstance(req, list) else set()


def _drift(trace_schema: dict[str, Any], contract_schema: dict[str, Any]) -> dict[str, Any]:
    """The structural differences between the run-time and committed schemas (empty: none)."""
    tp, cp = _props(trace_schema), _props(contract_schema)
    t_names, c_names = set(tp), set(cp)
    diff: dict[str, Any] = {}
    if added := sorted(t_names - c_names):
        diff["added_in_run"] = added  # the live tool has fields the committed contract lacks
    if gone := sorted(c_names - t_names):
        diff["gone_from_run"] = gone  # contract has fields the live tool no longer has
    if retyped := sorted(n for n in (t_names & c_names) if _prop_type(tp[n]) != _prop_type(cp[n])):
        diff["retyped"] = retyped
    if now_req := sorted(_required(trace_schema) - _required(contract_schema)):
        diff["now_required"] = now_req
    if not_req := sorted(_required(contract_schema) - _required(trace_schema)):
        diff["no_longer_required"] = not_req
    t_type, c_type = trace_schema.get("type"), contract_schema.get("type")
    if t_type != c_type:
        diff["type"] = {"run": t_type, "contract": c_type}
    return diff


def _summary(name: str, diff: dict[str, Any]) -> str:
    parts: list[str] = []
    if "added_in_run" in diff:
        parts.append("added in the run: " + ", ".join(diff["added_in_run"]))
    if "gone_from_run" in diff:
        parts.append("gone from the run: " + ", ".join(diff["gone_from_run"]))
    if "retyped" in diff:
        parts.append("retyped: " + ", ".join(diff["retyped"]))
    if "now_required" in diff:
        parts.append("newly required: " + ", ".join(diff["now_required"]))
    if "no_longer_required" in diff:
        parts.append("no longer required: " + ", ".join(diff["no_longer_required"]))
    if "type" in diff:
        parts.append(f"type {diff['type']['contract']} -> {diff['type']['run']}")
    return f"{name!r} schema drifted from the committed tools.json ({'; '.join(parts)})"


class ContractDriftRule(Rule):
    """R11: a tool's run-time schema (from the trace) differs from the committed tools.json."""

    id = "R11"
    finding_type = "contract_drift"

    def applicable(self, trace: Trace, registry: ToolRegistry) -> None:
        # Opt-in: fires only on a real difference between two property-bearing schemas (see run()).
        return None

    def run(self, trace: Trace, registry: ToolRegistry) -> list[Finding]:
        inline: dict[str, tuple[int, dict[str, Any]]] = {}
        for call in trace.tool_calls():
            if call.schema is not None and call.name not in inline:
                inline[call.name] = (call.index, call.schema)
        findings: list[Finding] = []
        for name, (idx, trace_schema) in inline.items():
            contract_schema = registry.schema_for(name)
            if contract_schema is None or not _props(trace_schema) or not _props(contract_schema):
                continue  # need two property-bearing schemas to compare
            diff = _drift(trace_schema, contract_schema)
            if diff:
                findings.append(
                    Finding(
                        rule=self.id,
                        finding_type=self.finding_type,
                        tier=ConfidenceTier.HARD_EVENT,
                        summary=_summary(name, diff),
                        evidence={"step_indices": [idx], "tool": name, "drift": diff},
                    )
                )
        return findings
