"""OpenHands SWE-bench trajectory -> native tracelint Trace (experiment, not core API).

SWE-bench ``experiments`` submissions export each OpenHands run as an **OpenAI chat message list**
(verified on the 2025 Claude-4-Sonnet and GPT-5 Verified submissions). Shape:

- ``{"role": "system", ...}`` — the agent prompt (ignored).
- ``{"role": "user", "content": [...]}`` — the issue (and any later continuation prompts).
- ``{"role": "assistant", "content": [text parts], "tool_calls": [{"id", "function": {"name",
  "arguments"(JSON string)}}]}`` — the thought (text) and the action(s).
- ``{"role": "tool", "tool_call_id", "name", "content": [...]}`` — the observation for that call.

The exit code is **not** a structured field in this export; it is in the observation TEXT, e.g.
``[The command completed with exit code 0.]``. We extract it with one documented regex (the last
marker in an observation is the final command's status) — this is the handoff's documented fallback,
and its coverage is reported by the runner.

Mapping (one trace per trajectory, ``run_id = "<submission>/<instance_id>"``):

- user message -> user :class:`Message`; assistant text and any ``think`` tool call -> assistant
  :class:`Message` (so R3's provenance sees what the agent reasoned about);
- every other tool call -> :class:`ToolCall` (``call_id`` = the OpenAI ``tool_calls[].id``, which
  pairs 1:1 with the ``tool`` message's ``tool_call_id``), args = the parsed ``arguments``;
- its observation -> :class:`ToolResult` with the full text as ``content`` (the exit-code marker is
  kept for audit) and ``status`` from the exit code: ``0 -> ok``, ``!= 0 -> error``, absent ->
  ``unknown``. R2a then fires on the structured status, not a string match.
"""

from __future__ import annotations

import json
import re
from typing import Any

from tracelint.trace import Message, ResultStatus, Role, ToolCall, ToolResult, Trace

#: Matches the exit-code marker OpenHands writes into a bash/editor observation. Several markers can
#: appear in one observation (``completed`` then ``finished``); the LAST is the final command's.
EXIT_CODE_RE = re.compile(r"exit code (-?\d+)")


def _text(content: Any) -> str:
    """Flatten a message ``content`` (a string, or a list of ``{type, text}`` parts) to text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"
        )
    return "" if content is None else str(content)


def extract_exit_code(observation: str) -> int | None:
    """The final exit code in an observation's text, or ``None`` if no marker is present."""
    matches = EXIT_CODE_RE.findall(observation or "")
    return int(matches[-1]) if matches else None


def _status_for(exit_code: int | None) -> ResultStatus:
    if exit_code is None:
        return ResultStatus.UNKNOWN
    return ResultStatus.OK if exit_code == 0 else ResultStatus.ERROR


def trajectory_to_trace(messages: list[dict], *, run_id: str) -> Trace:
    """Convert one OpenHands OpenAI-message trajectory to a native :class:`Trace`."""
    observation_for: dict[str, str] = {
        m["tool_call_id"]: _text(m.get("content"))
        for m in messages
        if m.get("role") == "tool" and m.get("tool_call_id")
    }

    steps: list = []
    for m in messages:
        role = m.get("role")
        if role == "user":
            text = _text(m.get("content"))
            if text.strip():
                steps.append(Message(role=Role.USER, content=text))
        elif role == "assistant":
            text = _text(m.get("content"))
            if text.strip():
                steps.append(Message(role=Role.ASSISTANT, content=text))
            for call in m.get("tool_calls") or []:
                fn = call.get("function") or {}
                name = str(fn.get("name") or "tool")
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except (ValueError, TypeError):
                    args = {}
                if not isinstance(args, dict):
                    args = {"value": args}
                call_id = call.get("id") or f"c{len(steps)}"
                if name == "think":
                    thought = args.get("thought")
                    if isinstance(thought, str) and thought.strip():
                        steps.append(Message(role=Role.ASSISTANT, content=thought))
                    continue
                steps.append(ToolCall(call_id=call_id, name=name, args=args))
                obs = observation_for.get(call_id, "")
                status = _status_for(extract_exit_code(obs))
                steps.append(ToolResult(call_id=call_id, content=obs, status=status))
        # system and tool messages carry no new step (tool obs are paired inline above)
    return Trace(run_id=run_id, steps=steps)


def load_trajectory(path: str, *, submission: str, instance_id: str) -> Trace:
    """Load a submission's ``trajs/<instance_id>.json`` and convert it to a :class:`Trace`."""
    with open(path, encoding="utf-8") as fh:
        messages = json.load(fh)
    return trajectory_to_trace(messages, run_id=f"{submission}/{instance_id}")
