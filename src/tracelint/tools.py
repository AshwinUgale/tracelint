"""Tool definitions the rules validate against (spec §II.4 / §II.5).

A ``ToolSpec`` bundles the three things different rules need about a tool:

- ``schema``   — the JSON Schema for its arguments (R1 validates recorded calls against it).
- ``metadata`` — behavioural hints (``idempotent`` / ``polling`` / ``paginated`` / ...) that let
  the loop and redundant-call rules (R4/R5) avoid flagging legitimate repetition (deep-design
  Trap 4), and let the error rules (R2) know which errors are expected-retryable.
- ``value_origins`` — optional per-field ``x-value-origin`` annotations (``provided`` /
  ``generated``) that gate R3's high-confidence hallucination tier (spec §II.5, R3). Absent by
  default, which is exactly why out-of-box hallucination detection is candidate-only.

If a tool is unknown to the registry, rules that need its schema/metadata **suppress** rather
than guess — the registry is a source of ground truth, and missing ground truth fails closed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tracelint.predicates import FailurePredicate

_REQUIREMENT_KEYS = {"tool", "succeeded", "same"}


@dataclass(frozen=True)
class Requirement:
    """A declared precondition (R9): before the tool runs, the latest call to ``tool`` must have
    returned successfully (with ``succeeded=False``, merely returned). ``same`` names arguments
    whose values must match between the two calls, scoping the check to one entity: one order, one
    build."""

    tool: str
    succeeded: bool = True
    same: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, data: Any, owner: str) -> Requirement:
        if not isinstance(data, dict):
            raise ValueError(
                f'{owner}: each requires entry is an object, e.g. {{"tool": "get_order"}}'
            )
        unknown = sorted(set(data) - _REQUIREMENT_KEYS)
        if unknown:
            raise ValueError(
                f"{owner}: unknown key {unknown[0]!r} in requires; expected tool, succeeded, same"
            )
        tool = data.get("tool")
        if not isinstance(tool, str) or not tool:
            raise ValueError(f"{owner}: a requires entry needs the required tool's name")
        succeeded = data.get("succeeded", True)
        if not isinstance(succeeded, bool):
            raise ValueError(f"{owner}: requires succeeded must be true or false")
        same = data.get("same", [])
        if not isinstance(same, list) or not all(isinstance(n, str) and n for n in same):
            raise ValueError(f"{owner}: requires same must be a list of argument names")
        return cls(tool, succeeded, tuple(same))

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"tool": self.tool}
        if not self.succeeded:
            out["succeeded"] = False
        if self.same:
            out["same"] = list(self.same)
        return out

    def describe(self) -> str:
        first = f"a successful {self.tool}" if self.succeeded else f"a {self.tool} call"
        return first + (f" (same {', '.join(self.same)})" if self.same else "")


@dataclass(frozen=True)
class ToolMetadata:
    """Behavioural hints about a tool (spec §II.5, "Tool metadata").

    Defaults are the conservative choice: a tool is assumed *not* idempotent and *not*
    side-effecting-safe to repeat unless declared, so nothing is waved through by omission.
    """

    idempotent: bool = False
    side_effecting: bool = False
    polling: bool = False
    paginated: bool = False
    retryable_errors: tuple[str, ...] = ()
    #: Declared domain-failure predicate — a result matching it is a structured error (R2), even
    #: when the transport reported success. See :mod:`tracelint.predicates`.
    failure_when: FailurePredicate | None = None
    #: Declared preconditions (R9): calls that must have succeeded before this tool runs.
    requires: tuple[Requirement, ...] = ()

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None, owner: str = "tool") -> ToolMetadata:
        if not data:
            return cls()
        requires = data.get("requires") or []
        if not isinstance(requires, list):
            raise ValueError(f'{owner}: requires must be a list, e.g. [{{"tool": "get_order"}}]')
        return cls(
            idempotent=bool(data.get("idempotent", False)),
            side_effecting=bool(data.get("side_effecting", False)),
            polling=bool(data.get("polling", False)),
            paginated=bool(data.get("paginated", False)),
            retryable_errors=tuple(data.get("retryable_errors", ()) or ()),
            failure_when=FailurePredicate.from_dict(data.get("failure_when")),
            requires=tuple(Requirement.from_dict(r, owner) for r in requires),
        )


@dataclass(frozen=True)
class ToolSpec:
    """Everything the rules know about one tool."""

    name: str
    schema: dict[str, Any] | None = None
    metadata: ToolMetadata = field(default_factory=ToolMetadata)
    schema_version: str | None = None
    value_origins: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Populate x-value-origin annotations from the schema when not passed explicitly, so a
        # directly-constructed ToolSpec behaves the same as one loaded via from_dict.
        if not self.value_origins:
            origins = _extract_value_origins(self.schema, None)
            if origins:
                object.__setattr__(self, "value_origins", origins)

    @classmethod
    def from_dict(cls, name: str, data: dict[str, Any]) -> ToolSpec:
        schema = data.get("schema") or data.get("input_schema") or data.get("parameters")
        return cls(
            name=name,
            schema=schema,
            metadata=ToolMetadata.from_dict(data.get("metadata"), owner=name),
            schema_version=data.get("schema_version"),
            value_origins=_extract_value_origins(schema, data.get("value_origins")),
        )


@dataclass(frozen=True)
class ToolContract:
    """A coherent, read-only view of a tool's declared *contract*.

    tracelint never infers a tool's behaviour from its name — an operator declares it. Those
    declarations accreted as separate keys (``schema``, ``side_effecting``, ``failure_when``, and
    per-field ``x-value-origin``), but they are one idea: the contract the tool operates under, that
    the rules check the recorded execution against. This groups them into named sections so the
    concept has a single home. It adds **no** semantics — it reads the same :class:`ToolSpec` the
    rules already use, so declaring a contract is still just the existing metadata keys.
    """

    name: str
    schema: dict[str, Any] | None
    metadata: ToolMetadata
    value_origins: dict[str, str]

    @classmethod
    def from_spec(cls, spec: ToolSpec) -> ToolContract:
        return cls(spec.name, spec.schema, spec.metadata, dict(spec.value_origins))

    @property
    def side_effecting(self) -> bool:
        return self.metadata.side_effecting

    @property
    def failure_when(self) -> FailurePredicate | None:
        return self.metadata.failure_when

    def to_dict(self) -> dict[str, Any]:
        m = self.metadata
        props = list((self.schema or {}).get("properties", {})) if self.schema else []
        return {
            "name": self.name,
            "schema": {"declared": self.schema is not None, "properties": sorted(props)},
            "effects": {
                "side_effecting": m.side_effecting,
                "idempotent": m.idempotent,
                "polling": m.polling,
                "paginated": m.paginated,
            },
            "failure_when": self.failure_when.summary() if self.failure_when else None,
            "requires": [r.to_dict() for r in m.requires],
            "provenance": dict(self.value_origins),
        }

    def describe(self) -> str:
        """A compact, human-readable block presenting the four contract sections."""
        m = self.metadata
        if self.schema is not None:
            n = len(list((self.schema.get("properties") or {}).keys()))
            args = f"schema declared ({n} propert{'y' if n == 1 else 'ies'})"
        else:
            args = "no schema declared"
        effects = "side-effecting" if m.side_effecting else "no declared side effect"
        if m.idempotent:
            effects += ", idempotent"
        failure = self.failure_when.summary() if self.failure_when else "none declared"
        requires = ", ".join(r.describe() for r in m.requires) or "none declared"
        prov = (
            ", ".join(f"{k}={v}" for k, v in sorted(self.value_origins.items()))
            or "none declared"
        )
        return "\n".join(
            [
                self.name,
                f"  args:       {args}",
                f"  effects:    {effects}",
                f"  failure:    {failure}",
                f"  requires:   {requires}",
                f"  provenance: {prov}",
            ]
        )


def _extract_value_origins(
    schema: dict[str, Any] | None, explicit: dict[str, str] | None
) -> dict[str, str]:
    """Pull ``x-value-origin`` annotations out of a schema's properties (spec §II.5, R3).

    An explicit ``value_origins`` map wins; otherwise read each property's ``x-value-origin``.
    """
    if explicit:
        return dict(explicit)
    origins: dict[str, str] = {}
    if schema and isinstance(schema.get("properties"), dict):
        for field_name, sub in schema["properties"].items():
            if isinstance(sub, dict) and "x-value-origin" in sub:
                origins[field_name] = sub["x-value-origin"]
    return origins


class ToolRegistry:
    """Name → :class:`ToolSpec`. The rules' source of ground truth about tools."""

    def __init__(self, tools: dict[str, ToolSpec] | None = None) -> None:
        self._tools: dict[str, ToolSpec] = dict(tools or {})

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def __len__(self) -> int:
        return len(self._tools)

    def add(self, spec: ToolSpec) -> None:
        self._tools[spec.name] = spec

    def names(self) -> list[str]:
        """The declared tool names (the ground truth R7 compares called tools against)."""
        return list(self._tools)

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def schema_for(self, name: str) -> dict[str, Any] | None:
        spec = self._tools.get(name)
        return spec.schema if spec else None

    def metadata_for(self, name: str) -> ToolMetadata | None:
        spec = self._tools.get(name)
        return spec.metadata if spec else None

    def contract_for(self, name: str) -> ToolContract | None:
        """The declared contract for ``name`` as one coherent view, or ``None`` if undeclared."""
        spec = self._tools.get(name)
        return ToolContract.from_spec(spec) if spec else None

    def contracts(self) -> list[ToolContract]:
        """Every declared tool's contract, in declaration order."""
        return [ToolContract.from_spec(s) for s in self._tools.values()]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ToolRegistry:
        """Load from ``{tool_name: {schema, metadata, ...}}`` or ``{"tools": {...}}``."""
        table = data.get("tools", data)
        return cls({name: ToolSpec.from_dict(name, spec) for name, spec in table.items()})

    @classmethod
    def load(cls, path: str | Path) -> ToolRegistry:
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))
