"""A minimal ReAct agent used to *generate* traces to lint (never part of the linter).

The agent is the system under test. It runs a pluggable LLM in a bounded decide/act/observe loop
and records a canonical :class:`~tracelint.trace.Trace`. The deterministic
:class:`ScriptedLLM` drives all tests and demos; :class:`OpenAILLM` (behind ``[real-agent]``) is
the real backend. See ``PROJECTS-TECHNICAL-SPEC.md`` §II.1 and learning-doc 01.
"""

from __future__ import annotations

from tracelint.agent.demo import (
    DEMO_TASK,
    build_demo_toolset,
    build_recovery_task,
    run_demo,
    run_faulted_demo,
    run_ignored_error_demo,
    run_loop_demo,
)
from tracelint.agent.react import LLM, FinalAnswer, Proposal, ReActAgent, ToolInvocation
from tracelint.agent.scripted import PolicyLLM, ScriptedLLM, final, tool
from tracelint.agent.tools import AgentTool, AgentToolset, ToolError

__all__ = [
    "ReActAgent",
    "LLM",
    "Proposal",
    "ToolInvocation",
    "FinalAnswer",
    "ScriptedLLM",
    "PolicyLLM",
    "tool",
    "final",
    "AgentTool",
    "AgentToolset",
    "ToolError",
    "build_demo_toolset",
    "run_demo",
    "run_ignored_error_demo",
    "run_loop_demo",
    "run_faulted_demo",
    "build_recovery_task",
    "DEMO_TASK",
]
