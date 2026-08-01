"""A real OpenAI backend for the ReAct agent (opt-in ``[real-agent]`` extra).

This is the only place a real model is used, and it is used solely to *generate* traces to lint —
never inside the linter's decision path (spec §II.1: the analysis is judge-free). It is not
exercised by CI; the deterministic :class:`~tracelint.agent.scripted.ScriptedLLM` covers the
tests. ``openai`` is imported lazily so the base install never requires it.
"""

from __future__ import annotations

import json
from typing import Any

from tracelint.agent.react import FinalAnswer, Proposal, ToolInvocation
from tracelint.trace import Message, Step, ToolCall, ToolResult


def _steps_to_openai_messages(steps: list[Step]) -> list[dict[str, Any]]:
    """Reconstruct an OpenAI message history from canonical steps (one call per assistant turn)."""
    messages: list[dict[str, Any]] = []
    for step in steps:
        if isinstance(step, Message):
            messages.append({"role": step.role.value, "content": step.content})
        elif isinstance(step, ToolCall):
            messages.append(
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": step.call_id,
                            "type": "function",
                            "function": {
                                "name": step.name,
                                "arguments": json.dumps(step.args),
                            },
                        }
                    ],
                }
            )
        elif isinstance(step, ToolResult):
            content = step.content
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": step.call_id,
                    "content": content if isinstance(content, str) else json.dumps(content),
                }
            )
    return messages


class OpenAILLM:
    """Proposes the next step by calling OpenAI chat-completions with tool definitions."""

    def __init__(
        self,
        model: str = "gpt-4o-mini",
        *,
        client: Any = None,
        temperature: float = 0.0,
    ) -> None:
        if client is None:
            try:
                from openai import OpenAI
            except ImportError as exc:  # pragma: no cover - exercised only without the extra
                raise ImportError(
                    "OpenAILLM requires the [real-agent] extra: pip install 'tracelint[real-agent]'"
                ) from exc
            client = OpenAI()
        self.client = client
        self.model = model
        self.temperature = temperature

    def propose(  # pragma: no cover
        self, steps: list[Step], tools: list[dict[str, Any]]
    ) -> Proposal:
        response = self.client.chat.completions.create(
            model=self.model,
            messages=_steps_to_openai_messages(steps),
            tools=tools or None,
            temperature=self.temperature,
        )
        choice = response.choices[0].message
        tool_calls = getattr(choice, "tool_calls", None)
        if tool_calls:
            call = tool_calls[0]
            try:
                args = json.loads(call.function.arguments or "{}")
            except (json.JSONDecodeError, ValueError):
                args = {}
            return ToolInvocation(
                name=call.function.name,
                args=args if isinstance(args, dict) else {},
                call_id=call.id,
            )
        return FinalAnswer(choice.content or "")
