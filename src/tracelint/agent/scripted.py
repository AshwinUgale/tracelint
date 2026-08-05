"""A deterministic, offline LLM backend that replays a fixed script (learning-doc 03 §2).

The whole test/validation path uses this: a scripted sequence of proposals makes the agent's
trace byte-reproducible, so the linter can be validated against *planted* behaviour (a clean run,
a schema-violating call, an ignored error, a loop) with no model, no network, and no flakiness.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from tracelint.agent.react import FinalAnswer, Proposal, ToolInvocation
from tracelint.trace import Step


class PolicyLLM:
    """A deterministic *reactive* backend: a policy function of the steps so far → a proposal.

    Unlike :class:`ScriptedLLM` (which replays a fixed sequence), a policy can branch on what it
    observed — e.g. stop safely when a tool returned an error — which is what makes recovery
    behaviour measurable without a real, nondeterministic model.
    """

    def __init__(self, policy: Callable[[list[Step]], Proposal]) -> None:
        self.policy = policy

    def propose(self, steps: list[Step], tools: list[dict[str, Any]]) -> Proposal:
        return self.policy(steps)


class ScriptedLLM:
    """Yields pre-set proposals in order, ignoring the conversation (deterministic)."""

    def __init__(self, script: list[Proposal]) -> None:
        self._script: list[Proposal] = list(script)
        self._i = 0

    def propose(self, steps: list[Step], tools: list[dict[str, Any]]) -> Proposal:
        if self._i >= len(self._script):
            # Exhausted scripts terminate cleanly rather than loop against the step cap.
            return FinalAnswer("(script exhausted)")
        proposal = self._script[self._i]
        self._i += 1
        return proposal


def tool(
    name: str,
    args: dict[str, Any] | None = None,
    *,
    call_id: str | None = None,
    thought: str | None = None,
) -> ToolInvocation:
    """Terse helper for building scripts: ``tool("cancel_order", {"order_id": "4521"})``."""
    return ToolInvocation(name=name, args=args or {}, call_id=call_id, thought=thought)


def final(text: str) -> FinalAnswer:
    """Terse helper for a final-answer step in a script."""
    return FinalAnswer(text)
