"""Canonical trace schema (spec §II.4).

The linter is only as good as the trace it reads (deep-design Trap 1: *trace-capture
fidelity*). Every provider/framework emits a different, lossy shape; adapters normalize into
the small, explicit vocabulary defined here so the rules never have to know where a trace came
from. The load-bearing rule is downstream (``rules.base``): if a trace lacks a field a check
needs, that check is **suppressed with a stated reason** — never run on partial data.

A ``Trace`` is an ordered list of ``Step`` s, each one of:

- ``Message``     — a role-tagged turn (``user`` / ``assistant`` / ``system`` / ``tool``).
- ``ToolCall``    — the agent *requesting* a tool by name with an ``args`` object.
- ``ToolResult``  — the tool's return, tagged with a coarse ``status`` (ok / error / unknown).

``ToolCall`` and ``ToolResult`` are paired by a shared ``call_id`` (§II.4's ``id``), the same
way native tool-calling APIs match a ``tool_call`` to its ``tool_result``. ``StepMeta`` carries
the operational metadata (timings, model, and the fault-injection tags) later phases need for
the recovery scorecard; it is optional and absent by default.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class Role(str, Enum):
    """The author of a :class:`Message` turn."""

    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
    TOOL = "tool"

    @classmethod
    def parse(cls, value: str) -> Role:
        try:
            return cls(value)
        except ValueError as exc:  # pragma: no cover - defensive
            raise ValueError(f"unknown message role {value!r}") from exc


class ResultStatus(str, Enum):
    """Coarse outcome class of a :class:`ToolResult`.

    Only ``ERROR`` from a *structured* signal (an explicit status field, ``http_status >= 400``,
    a structured exception) is deterministic enough to treat as a hard error event later (R2a);
    ``UNKNOWN`` means the adapter could not decide and forces the error rules to fall back to
    their heuristic, candidate tier rather than assert.
    """

    OK = "ok"
    ERROR = "error"
    UNKNOWN = "unknown"

    @classmethod
    def parse(cls, value: str | None) -> ResultStatus:
        if value is None:
            return cls.UNKNOWN
        try:
            return cls(value)
        except ValueError:
            return cls.UNKNOWN


@dataclass
class StepMeta:
    """Operational metadata for a step (spec §II.4).

    Optional throughout the MVP. ``injected`` / ``fault_injection_id`` are set by the fault
    injector (a later phase) so an injected failure is reproducible and never mistaken for an
    organic one; ``retry_attempt`` distinguishes a deliberate retry from a stuck loop.
    """

    timestamp: float | None = None
    duration_ms: float | None = None
    model: str | None = None
    provider: str | None = None
    model_version: str | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    parent_span_id: str | None = None
    retry_attempt: int | None = None
    fault_injection_id: str | None = None
    injected: bool = False
    tool_schema_version: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v is not None and v is not False}

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> StepMeta | None:
        if not data:
            return None
        known = {f for f in cls.__dataclass_fields__}  # noqa: C416 - explicit set of field names
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class SourceRef:
    """Where a step came from in its origin platform (provider-neutral).

    Adapters populate this so a finding can be traced back to the exact provider record — a
    Langfuse ``observation_id``, an OTel/Phoenix ``span_id`` — which is what lets an *integration*
    (not the rules) attach the finding to the offending span/observation and build a deep link.
    Optional and absent by default; the rule engine never reads it (rules speak only in step
    indices), keeping provider knowledge out of the deterministic core.
    """

    provider: str | None = None
    trace_id: str | None = None
    span_id: str | None = None
    observation_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v is not None}

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> SourceRef | None:
        if not data:
            return None
        known = {f for f in cls.__dataclass_fields__}  # noqa: C416 - explicit set of field names
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class Message:
    """A role-tagged conversational turn. ``content`` is free text (may be empty)."""

    role: Role
    content: str = ""
    index: int = -1
    meta: StepMeta | None = None
    source: SourceRef | None = None

    kind = "message"

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"type": self.kind, "role": self.role.value, "content": self.content}
        if self.meta is not None:
            out["meta"] = self.meta.to_dict()
        if self.source is not None:
            out["source"] = self.source.to_dict()
        return out


@dataclass
class ToolCall:
    """The agent requesting a tool.

    ``call_id`` pairs this call with its :class:`ToolResult`. ``args`` is the parsed argument
    object exactly as the model emitted it (before any transport-layer coercion), so a schema
    check (R1) validates what the *model* produced, not what a runtime later fixed up.
    ``raw_text`` preserves the original serialized call for evidence when parsing was lossy.
    ``schema`` is the tool's declared argument JSON Schema when the trace itself carries it (e.g.
    OpenInference ``tool.parameters`` / ``llm.tools.*.tool.json_schema``); it is discovery-only —
    rules validate against the operator's ``tools.json``, not this — and it lets ``tracelint init``
    bootstrap a starter contract.
    """

    call_id: str
    name: str
    args: dict[str, Any] = field(default_factory=dict)
    raw_text: str | None = None
    index: int = -1
    meta: StepMeta | None = None
    source: SourceRef | None = None
    schema: dict[str, Any] | None = None

    kind = "tool_call"

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "type": self.kind,
            "call_id": self.call_id,
            "name": self.name,
            "args": self.args,
        }
        if self.raw_text is not None:
            out["raw_text"] = self.raw_text
        if self.schema is not None:
            out["schema"] = self.schema
        if self.meta is not None:
            out["meta"] = self.meta.to_dict()
        if self.source is not None:
            out["source"] = self.source.to_dict()
        return out


@dataclass
class ToolResult:
    """A tool's return for a prior :class:`ToolCall` (matched by ``call_id``).

    ``status`` is the coarse class an adapter assigns; ``error`` and ``http_status`` retain the
    structured error signals R2a needs to tier a finding as a hard event versus a candidate.
    """

    call_id: str
    content: Any = None
    status: ResultStatus = ResultStatus.UNKNOWN
    error: str | None = None
    http_status: int | None = None
    index: int = -1
    meta: StepMeta | None = None
    source: SourceRef | None = None

    kind = "tool_result"

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "type": self.kind,
            "call_id": self.call_id,
            "content": self.content,
            "status": self.status.value,
        }
        if self.error is not None:
            out["error"] = self.error
        if self.http_status is not None:
            out["http_status"] = self.http_status
        if self.meta is not None:
            out["meta"] = self.meta.to_dict()
        if self.source is not None:
            out["source"] = self.source.to_dict()
        return out


Step = Message | ToolCall | ToolResult

_STEP_TYPES: dict[str, Any] = {
    Message.kind: Message,
    ToolCall.kind: ToolCall,
    ToolResult.kind: ToolResult,
}


def _step_from_dict(data: dict[str, Any]) -> Step:
    if not isinstance(data, dict):
        raise ValueError(f"each step must be a JSON object, got {type(data).__name__}")
    kind = data.get("type")
    meta = StepMeta.from_dict(data.get("meta"))
    source = SourceRef.from_dict(data.get("source"))
    if kind == Message.kind:
        return Message(
            role=Role.parse(data["role"]),
            content=data.get("content", ""),
            meta=meta,
            source=source,
        )
    if kind == ToolCall.kind:
        args = data.get("args") or {}
        if not isinstance(args, dict):
            raise ValueError(
                f"tool_call 'args' must be an object, got {type(args).__name__}"
            )
        return ToolCall(
            call_id=str(data["call_id"]),
            name=data["name"],
            args=args,
            raw_text=data.get("raw_text"),
            meta=meta,
            source=source,
        )
    if kind == ToolResult.kind:
        return ToolResult(
            call_id=str(data["call_id"]),
            content=data.get("content"),
            status=ResultStatus.parse(data.get("status")),
            error=data.get("error"),
            http_status=data.get("http_status"),
            meta=meta,
            source=source,
        )
    raise ValueError(f"unknown step type {kind!r} (expected message, tool_call, or tool_result)")


@dataclass
class Trace:
    """One agent run: an ordered list of steps plus the final output (spec §II.4).

    Steps are indexed sequentially at construction so a finding can cite an exact location
    (``step.index``). Tool calls and results are paired by ``call_id``; a call with no matching
    result is a real, observable state (the run ended, or the result was never captured) and is
    surfaced as such rather than hidden.
    """

    run_id: str
    steps: list[Step] = field(default_factory=list)
    final: Any = None

    def __post_init__(self) -> None:
        for i, step in enumerate(self.steps):
            step.index = i

    # --- iteration / filtering -------------------------------------------------------
    def __iter__(self) -> Iterator[Step]:
        return iter(self.steps)

    def __len__(self) -> int:
        return len(self.steps)

    def messages(self) -> list[Message]:
        return [s for s in self.steps if isinstance(s, Message)]

    def tool_calls(self) -> list[ToolCall]:
        return [s for s in self.steps if isinstance(s, ToolCall)]

    def tool_results(self) -> list[ToolResult]:
        return [s for s in self.steps if isinstance(s, ToolResult)]

    # --- call/result pairing ---------------------------------------------------------
    def result_for(self, call: ToolCall) -> ToolResult | None:
        """The first result after ``call`` sharing its ``call_id`` (``None`` if none)."""
        for step in self.steps[call.index + 1 :]:
            if isinstance(step, ToolResult) and step.call_id == call.call_id:
                return step
        return None

    def call_for(self, result: ToolResult) -> ToolCall | None:
        """The most recent call before ``result`` sharing its ``call_id`` (``None`` if none)."""
        for step in reversed(self.steps[: result.index]):
            if isinstance(step, ToolCall) and step.call_id == result.call_id:
                return step
        return None

    def pairs(self) -> list[tuple[ToolCall, ToolResult | None]]:
        """Every tool call with its matched result (or ``None`` when unmatched)."""
        return [(call, self.result_for(call)) for call in self.tool_calls()]

    # --- serialization ---------------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "steps": [s.to_dict() for s in self.steps],
            "final": self.final,
        }

    def to_json(self, *, indent: int | None = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Trace:
        if not isinstance(data, dict):
            raise ValueError(
                f"a trace must be a JSON object with a 'steps' list, got {type(data).__name__}"
            )
        raw_steps = data.get("steps", [])
        if not isinstance(raw_steps, list):
            raise ValueError(f"'steps' must be a list, got {type(raw_steps).__name__}")
        steps = [_step_from_dict(s) for s in raw_steps]
        return cls(run_id=str(data.get("run_id", "")), steps=steps, final=data.get("final"))

    @classmethod
    def from_json(cls, text: str) -> Trace:
        return cls.from_dict(json.loads(text))

    @classmethod
    def load(cls, path: str | Path) -> Trace:
        return cls.from_json(Path(path).read_text(encoding="utf-8"))


def load_traces(path: str | Path) -> list[Trace]:
    """Load one trace (``.json``) or many (``.jsonl``, one trace object per line)."""
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    if p.suffix == ".jsonl":
        return [Trace.from_json(line) for line in text.splitlines() if line.strip()]
    data = json.loads(text)
    if isinstance(data, list):
        return [Trace.from_dict(d) for d in data]
    return [Trace.from_dict(data)]


def build_trace(run_id: str, steps: Iterable[Step], final: Any = None) -> Trace:
    """Small convenience constructor used by adapters and tests."""
    return Trace(run_id=run_id, steps=list(steps), final=final)
