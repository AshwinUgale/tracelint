"""A minimal ReAct agent loop that records a canonical trace (learning-doc 01 §1–2).

This is the *system under test*, not part of the linter: its only job is to produce realistic
traces to lint. The loop is the textbook ``decide → act → observe`` cycle — the LLM proposes
either a tool call or a final answer; the harness executes proposed tool calls (the model never
executes anything itself) and appends the observation; repeat until a final answer or the step
cap. The step cap is the single most important defensive limit (learning-doc 01 §1): a model that
never emits a final answer must still terminate.

The ``LLM`` is pluggable: :class:`~tracelint.agent.scripted.ScriptedLLM` (deterministic, offline,
used by the whole test/CI path) or :class:`~tracelint.agent.openai_llm.OpenAILLM` (behind the
``[real-agent]`` extra). Both speak the same tiny protocol: ``propose(steps, tools) -> Proposal``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from tracelint.agent.tools import AgentToolset
from tracelint.trace import Message, Role, Step, ToolCall, Trace


@dataclass
class ToolInvocation:
    """A proposal to call one tool. ``call_id`` is assigned by the harness if left ``None``."""

    name: str
    args: dict[str, Any] = field(default_factory=dict)
    call_id: str | None = None
    thought: str | None = None


@dataclass
class FinalAnswer:
    """A proposal to stop and return ``text`` as the run's final answer."""

    text: str


Proposal = ToolInvocation | FinalAnswer


class LLM(Protocol):
    """The decision function the agent loop calls each step."""

    def propose(self, steps: list[Step], tools: list[dict[str, Any]]) -> Proposal: ...


class ReActAgent:
    """Runs an :class:`LLM` in a bounded ``decide → act → observe`` loop, recording a trace."""

    def __init__(
        self,
        llm: LLM,
        toolset: AgentToolset,
        *,
        max_steps: int = 8,
        system: str | None = None,
    ) -> None:
        self.llm = llm
        self.toolset = toolset
        self.max_steps = max_steps
        self.system = system

    def run(self, task: str, *, run_id: str = "agent-run") -> Trace:
        steps: list[Step] = []
        if self.system:
            steps.append(Message(Role.SYSTEM, self.system))
        steps.append(Message(Role.USER, task))

        for i in range(self.max_steps):
            # Index steps so a scripted/real LLM converting them sees stable positions.
            trace_view = Trace(run_id=run_id, steps=steps)
            proposal = self.llm.propose(trace_view.steps, self.toolset.to_openai_tools())

            if isinstance(proposal, FinalAnswer):
                steps.append(Message(Role.ASSISTANT, proposal.text))
                return Trace(run_id=run_id, steps=steps, final=proposal.text)

            call_id = proposal.call_id or f"call_{i + 1}"
            if proposal.thought:
                steps.append(Message(Role.ASSISTANT, proposal.thought))
            call = ToolCall(call_id=call_id, name=proposal.name, args=dict(proposal.args))
            steps.append(call)
            steps.append(self.toolset.execute(call))

        # Step cap reached with no final answer: an incomplete run (final stays None).
        return Trace(run_id=run_id, steps=steps, final=None)
