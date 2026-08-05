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

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> ToolMetadata:
        if not data:
            return cls()
        return cls(
            idempotent=bool(data.get("idempotent", False)),
            side_effecting=bool(data.get("side_effecting", False)),
            polling=bool(data.get("polling", False)),
            paginated=bool(data.get("paginated", False)),
            retryable_errors=tuple(data.get("retryable_errors", ()) or ()),
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
            metadata=ToolMetadata.from_dict(data.get("metadata")),
            schema_version=data.get("schema_version"),
            value_origins=_extract_value_origins(schema, data.get("value_origins")),
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

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ToolRegistry:
        """Load from ``{tool_name: {schema, metadata, ...}}`` or ``{"tools": {...}}``."""
        table = data.get("tools", data)
        return cls({name: ToolSpec.from_dict(name, spec) for name, spec in table.items()})

    @classmethod
    def load(cls, path: str | Path) -> ToolRegistry:
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))
