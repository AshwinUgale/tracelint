"""Tools an agent can call, and how a call's outcome becomes a canonical result.

The same :class:`AgentTool` definition feeds three consumers, which is the point: the agent uses
``schema`` to advertise the tool and ``func`` to execute it, and ``to_registry`` hands the very
same ``schema`` + ``metadata`` to the linter as ground truth. One definition, no drift between
what the agent was told and what the linter checks against.

A tool signals failure by **raising**: a plain exception becomes an ``error`` result, and a
:class:`ToolError` additionally carries an ``http_status`` so the error is a *structured* signal
(which R2a can later tier as a ``hard_event`` rather than a heuristic candidate).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from tracelint.tools import ToolMetadata, ToolRegistry, ToolSpec
from tracelint.trace import ResultStatus, ToolCall, ToolResult


class ToolError(Exception):
    """A tool failure carrying a structured ``http_status`` (e.g. 500, 429)."""

    def __init__(self, message: str, *, http_status: int | None = None) -> None:
        super().__init__(message)
        self.http_status = http_status


@dataclass
class AgentTool:
    """A callable tool: a name, an argument schema, an implementation, and behaviour hints."""

    name: str
    description: str
    schema: dict[str, Any]
    func: Callable[[dict[str, Any]], Any]
    metadata: ToolMetadata = field(default_factory=ToolMetadata)


class AgentToolset:
    """A named set of :class:`AgentTool` s, with executors and exporters."""

    def __init__(self, tools: list[AgentTool]) -> None:
        self._tools: dict[str, AgentTool] = {t.name: t for t in tools}

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def names(self) -> list[str]:
        return list(self._tools)

    def execute(self, call: ToolCall) -> ToolResult:
        """Run ``call`` and capture its outcome as a canonical :class:`ToolResult`."""
        tool = self._tools.get(call.name)
        if tool is None:
            return ToolResult(
                call_id=call.call_id,
                content=f"unknown tool {call.name!r}",
                status=ResultStatus.ERROR,
                error="unknown_tool",
            )
        try:
            content = tool.func(dict(call.args))
        except ToolError as exc:
            return ToolResult(
                call_id=call.call_id,
                content=str(exc),
                status=ResultStatus.ERROR,
                error=str(exc),
                http_status=exc.http_status,
            )
        except Exception as exc:  # noqa: BLE001 - the agent must observe any tool failure
            return ToolResult(
                call_id=call.call_id,
                content=str(exc),
                status=ResultStatus.ERROR,
                error=f"{type(exc).__name__}: {exc}",
            )
        return ToolResult(call_id=call.call_id, content=content, status=ResultStatus.OK)

    def to_registry(self) -> ToolRegistry:
        """Export schemas + metadata as the linter's ground truth."""
        return ToolRegistry(
            {
                t.name: ToolSpec(name=t.name, schema=t.schema, metadata=t.metadata)
                for t in self._tools.values()
            }
        )

    def to_openai_tools(self) -> list[dict[str, Any]]:
        """Export OpenAI-format tool definitions for a real LLM backend."""
        return [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.schema,
                },
            }
            for t in self._tools.values()
        ]
