"""OpenHands SWE-bench trajectory -> native tracelint Trace (experiment, not core API).

SWE-bench ``experiments`` submissions export each OpenHands run as a chat message list. Two
encodings
appear (verified on the 2025 Verified submissions), dispatched automatically:

1. **OpenAI tool-calling** (Claude-4-Sonnet, GPT-5): action in an assistant message's
   ``tool_calls[].function.{name, arguments(JSON)}``; observation in a paired ``tool`` message
   matched
   by ``tool_call_id``; thought in assistant ``content`` text parts.
2. **OpenHands text format** (weaker models, e.g. Devstral): the action is in the assistant's
   text as
   ``<function=NAME><parameter=KEY>VALUE</parameter></function>``; the observation is the *next*
   ``user``
   message (``EXECUTION RESULT of [NAME]: ...``); thought is the assistant text before the block.

The exit code is in the observation TEXT (``[The command completed with exit code N.]``), not a
structured field, so it is read with one regex (last marker = the final command's status) — coverage
is reported by the runner.

Mapping (one trace per trajectory, ``run_id = "<submission>/<instance_id>"``):

- ``user`` / issue -> user :class:`Message`; assistant text and any ``think`` call -> assistant
  :class:`Message` (so R3's provenance sees the reasoning);
- every other tool call -> :class:`ToolCall`; ``str_replace_editor`` is split by its ``command``
  sub-field into ``str_replace_editor.<command>`` so a write (``str_replace``/``insert``/``create``)
  can be declared side-effecting while ``view`` is not;
- its observation -> :class:`ToolResult` (full text kept for audit) with ``status`` from the exit
  code
  (``0 -> ok``, ``!= 0 -> error``, absent -> ``unknown``), so R2a fires on the structured status.
"""

from __future__ import annotations

import json
import re
from typing import Any

from tracelint.trace import Message, ResultStatus, Role, ToolCall, ToolResult, Trace

#: Exit-code marker OpenHands writes into an observation; several can appear, the LAST is the final.
EXIT_CODE_RE = re.compile(r"exit code (-?\d+)")
#: Text-format action block and its parameters.
_FUNCTION_RE = re.compile(r"<function=([\w.]+)>(.*?)</function>", re.DOTALL)
_PARAM_RE = re.compile(r"<parameter=([\w.]+)>(.*?)</parameter>", re.DOTALL)


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


def tool_name(name: str, args: dict) -> str:
    """Split ``str_replace_editor`` by its ``command`` so edits can be side-effecting, not views."""
    if name == "str_replace_editor":
        command = args.get("command")
        if isinstance(command, str) and command:
            return f"str_replace_editor.{command}"
    return name


def _append_call(steps: list, call_id: str, name: str, args: dict, observation: str) -> None:
    if name == "think":
        thought = args.get("thought")
        if isinstance(thought, str) and thought.strip():
            steps.append(Message(role=Role.ASSISTANT, content=thought))
        return
    steps.append(ToolCall(call_id=call_id, name=tool_name(name, args), args=args))
    status = _status_for(extract_exit_code(observation))
    steps.append(ToolResult(call_id=call_id, content=observation, status=status))


def _from_openai(messages: list[dict], run_id: str) -> Trace:
    observation_for = {
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
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except (ValueError, TypeError):
                    args = {}
                if not isinstance(args, dict):
                    args = {"value": args}
                call_id = call.get("id") or f"c{len(steps)}"
                name = str(fn.get("name") or "tool")
                _append_call(steps, call_id, name, args, observation_for.get(call_id, ""))
    return Trace(run_id=run_id, steps=steps)


def _coerce(value: str) -> Any:
    """Text-format params are strings; parse an array/object value (e.g. ``view_range``) as JSON so
    it is typed like the OpenAI format, leaving plain strings (commands, paths, code) untouched."""
    value = value.strip()
    if value[:1] in "[{":
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            pass
    return value


def _parse_text_actions(text: str) -> list[tuple[str, dict]]:
    actions = []
    for match in _FUNCTION_RE.finditer(text):
        args = {k: _coerce(v) for k, v in _PARAM_RE.findall(match.group(2))}
        actions.append((match.group(1), args))
    return actions


def _from_text(messages: list[dict], run_id: str) -> Trace:
    steps: list = []
    seen_issue = False
    index = 0
    for i, m in enumerate(messages):
        role = m.get("role")
        if role == "user":
            if not seen_issue:
                text = _text(m.get("content"))
                if text.strip():
                    steps.append(Message(role=Role.USER, content=text))
                    seen_issue = True
            # later user messages are observations, consumed by the action before them
        elif role == "assistant":
            text = _text(m.get("content"))
            thought = text.split("<function=", 1)[0].strip()
            if thought:
                steps.append(Message(role=Role.ASSISTANT, content=thought))
            following = _text(messages[i + 1].get("content")) if i + 1 < len(messages) else ""
            for name, args in _parse_text_actions(text):
                _append_call(steps, f"c{index}", name, args, following)
                index += 1
                following = ""  # a second action in the same turn has no recorded observation
    return Trace(run_id=run_id, steps=steps)


def trajectory_to_trace(messages: list[dict], *, run_id: str) -> Trace:
    """Convert one OpenHands trajectory to a native :class:`Trace`, auto-detecting the encoding."""
    has_tool_calls = any(isinstance(m, dict) and m.get("tool_calls") for m in messages)
    builder = _from_openai if has_tool_calls else _from_text
    return builder(messages, run_id)


def load_trajectory(path: str, *, submission: str, instance_id: str) -> Trace:
    """Load a submission's ``trajs/<instance_id>.json`` and convert it to a :class:`Trace`."""
    with open(path, encoding="utf-8") as fh:
        messages = json.load(fh)
    return trajectory_to_trace(messages, run_id=f"{submission}/{instance_id}")
