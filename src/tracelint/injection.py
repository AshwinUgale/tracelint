"""A thin, seeded fault injector (spec §II.6; learning-doc 03 §2).

The injector's only job is to *generate* failure traces to lint and to feed the recovery scorecard
(Phase 6). It is injected at a **boundary** — it wraps an
:class:`~tracelint.agent.tools.AgentToolset` and exposes the same ``execute`` interface, so the
same agent code runs against the real toolset in
tests and against the injecting wrapper in the harness, with no ``if testing`` branch inside the
agent (learning-doc 03 §2, injection points).

Every injection decision is driven by a **seeded** RNG in a fixed order, so re-running with the same
seed reproduces the exact same fault pattern (03 §2, deterministic seeding). Every injected result
is tagged (``StepMeta.injected`` + ``fault_injection_id``) so it is never mistaken for an organic
failure. The taxonomy follows 03 §2: the silent faults (empty / malformed / truncated / wrong
schema) are the sharpest test because they do not trigger exception-handling paths.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol

from tracelint.agent.tools import AgentToolset
from tracelint.trace import ResultStatus, StepMeta, ToolCall, ToolResult


class FaultType(str, Enum):
    """The fault taxonomy for tool-using systems (learning-doc 03 §2)."""

    TIMEOUT = "timeout"
    ERROR = "error"  # HTTP 500-style hard error
    RATE_LIMIT = "rate_limit"  # HTTP 429
    MALFORMED_JSON = "malformed_json"  # "succeeds" but does not parse
    EMPTY = "empty"  # valid-but-empty; silent
    TRUNCATED = "truncated"  # partial result; silent
    WRONG_SCHEMA = "wrong_schema"  # right status, wrong shape; silent


_NEEDS_ORIGINAL = {FaultType.TRUNCATED}


def _truncate(content: Any) -> Any:
    if isinstance(content, str):
        return content[: max(1, len(content) // 2)]
    if isinstance(content, list):
        return content[: max(0, len(content) // 2)]
    if isinstance(content, dict):
        keep = list(content)[: max(0, len(content) // 2)]
        return {k: content[k] for k in keep}
    return str(content)[: max(1, len(str(content)) // 2)]


def apply_fault(fault: FaultType, call: ToolCall, original: ToolResult | None = None) -> ToolResult:
    """Render ``fault`` as a canonical :class:`ToolResult` for ``call``."""
    cid = call.call_id
    if fault is FaultType.TIMEOUT:
        return ToolResult(cid, "request timed out", status=ResultStatus.ERROR, error="timeout")
    if fault is FaultType.ERROR:
        return ToolResult(
            cid, "internal server error", status=ResultStatus.ERROR, error="server_error",
            http_status=500,
        )
    if fault is FaultType.RATE_LIMIT:
        return ToolResult(
            cid, "rate limited", status=ResultStatus.ERROR, error="rate_limited", http_status=429
        )
    if fault is FaultType.MALFORMED_JSON:
        return ToolResult(cid, '{"incomplete": ', status=ResultStatus.OK)
    if fault is FaultType.EMPTY:
        return ToolResult(cid, [], status=ResultStatus.OK)
    if fault is FaultType.TRUNCATED:
        body = _truncate(original.content if original else "")
        return ToolResult(cid, body, status=ResultStatus.OK)
    if fault is FaultType.WRONG_SCHEMA:
        return ToolResult(cid, {"unexpected_field": True}, status=ResultStatus.OK)
    raise ValueError(f"unknown fault type {fault!r}")  # pragma: no cover


class InjectionPlan(Protocol):
    """Decides whether to inject a fault for a given call."""

    def decide(
        self, call: ToolCall, ordinal: int, tool_ordinal: int, rng: random.Random
    ) -> FaultType | None: ...


@dataclass
class TargetedInjection:
    """Inject one fault on the ``occurrence``-th call to ``tool`` (``None`` = any tool)."""

    fault: FaultType
    tool: str | None = None
    occurrence: int = 1

    def decide(self, call, ordinal, tool_ordinal, rng):
        if (self.tool is None or call.name == self.tool) and tool_ordinal == self.occurrence:
            return self.fault
        return None


@dataclass
class RandomInjection:
    """Inject a random fault from ``faults`` on each call with probability ``rate`` (seeded)."""

    faults: list[FaultType]
    rate: float = 0.3

    def decide(self, call, ordinal, tool_ordinal, rng):
        if self.faults and rng.random() < self.rate:
            return self.faults[rng.randrange(len(self.faults))]
        return None


class FaultInjector:
    """Wraps an :class:`AgentToolset`, injecting faults per a seeded plan (a drop-in toolset)."""

    def __init__(self, toolset: AgentToolset, plan: InjectionPlan, *, seed: int = 0) -> None:
        self.toolset = toolset
        self.plan = plan
        self.rng = random.Random(seed)
        self._global = 0
        self._per_tool: dict[str, int] = {}

    def execute(self, call: ToolCall) -> ToolResult:
        self._global += 1
        self._per_tool[call.name] = self._per_tool.get(call.name, 0) + 1
        fault = self.plan.decide(call, self._global, self._per_tool[call.name], self.rng)
        if fault is None:
            return self.toolset.execute(call)
        original = self.toolset.execute(call) if fault in _NEEDS_ORIGINAL else None
        result = apply_fault(fault, call, original)
        result.meta = StepMeta(injected=True, fault_injection_id=f"{fault.value}@{self._global}")
        return result

    # Delegate the rest of the toolset interface so the agent treats this as its toolset.
    def to_openai_tools(self) -> list[dict[str, Any]]:
        return self.toolset.to_openai_tools()

    def to_registry(self):
        return self.toolset.to_registry()

    def names(self) -> list[str]:
        return self.toolset.names()
