"""ATIF reader — Harbor's Agent Trajectory Interchange Format (spec §II.4).

ATIF (``rfcs/0001-trajectory-format.md`` in harbor-framework/harbor) is the JSON format Harbor
writes for every agent run (``agent/trajectory.json``), whichever agent produced it — Claude Code,
Codex, Gemini CLI, OpenHands, Terminus — so one reader covers every agent Harbor runs. A
trajectory is ``{schema_version, agent, steps[]}``; each step is a ``system`` prompt, a ``user``
message, or one ``agent`` turn (its message, its ``tool_calls``, and the ``observation`` they got).

Mapping into a canonical :class:`Trace`:

- a ``system`` / ``user`` step's ``message``                 → :class:`Message`
- an ``agent`` step's ``reasoning_content`` and ``message``  → assistant :class:`Message`
- each of its ``tool_calls[]``                               → :class:`ToolCall`
- each ``observation.results[]`` naming a ``source_call_id``  → :class:`ToolResult`, paired by it
- a result with no ``source_call_id`` (a non-tool action, or a system operation such as a context
  summary)                                                    → a system :class:`Message`: what the
  agent was shown, never a tool call the trace does not record — except the one observation a turn
  records for its whole batch of calls (Harbor's Terminus: one terminal screen per batch of
  keystrokes), which is paired with the turn's last call

Two details keep a valid run from reading as a defective one. A step marked ``is_copied_context``
was copied in from an earlier context window (after a summary): its tool calls are not new actions,
so they are not replayed — doing so would invent repeats and loops — while its text stays as
context. An embedded subagent (``subagent_trajectories``, v1.7) is its own run with its own steps,
so :func:`from_atif_trajectories` yields it as a separate trace rather than splicing it in.

**Result status.** ATIF has no result-status field, so each producer records a failed tool its own
way. Only structured signals are read — never a guess from free text (that heuristic is R2a's,
tiered as a candidate):

- failed: ``extra.is_error`` or ``extra.tool_result_is_error`` is ``true`` (Harbor's Kimi / Pi and
  Claude Code converters), the line ``[error] tool reported failure`` that Harbor's Claude Code
  converter appends to a failed result, ``extra.status`` of ``error`` / ``failed`` / ``failure``
  (Strands), or a truthy ``error`` field in the result itself;
- succeeded: one of those flags ``false``, ``extra.status`` of ``success`` / ``ok``, or an exit
  code of ``0`` in ``extra.exit_code`` / ``return_code`` / ``returncode``;
- otherwise unknown. A non-zero exit code is not read as a failure: ``grep`` with no match, or a
  reproduction script that is meant to fail, exits non-zero on a run that is going fine.

Tool definitions in ``agent.tool_definitions`` (OpenAI function format, v1.5+) are attached to each
call as ``ToolCall.schema`` for ``tracelint init`` and R11, like the other adapters' discovered
schemas; :func:`atif_tools_to_registry` turns them into a contract when you want the trajectory's
own definitions to be the one R1 validates against.
"""

from __future__ import annotations

import json
import re
from typing import Any

from tracelint.adapters._common import content_error, model_call_args, tool_result_content
from tracelint.adapters.openai import openai_tools_to_registry
from tracelint.tools import ToolRegistry
from tracelint.trace import (
    Message,
    ResultStatus,
    Role,
    Step,
    StepMeta,
    ToolCall,
    ToolResult,
    Trace,
)

# The line Harbor's Claude Code converter appends to a result whose tool reported ``is_error``.
_ERROR_MARKER = re.compile(r"(?m)^\[error\] tool reported failure\s*$")
_ERROR_FLAGS = ("is_error", "tool_result_is_error")
_EXIT_CODE_KEYS = ("exit_code", "return_code", "returncode")
_ERROR_STATUSES = frozenset({"error", "failed", "failure"})
_OK_STATUSES = frozenset({"success", "ok"})


def is_atif(doc: Any) -> bool:
    """Whether ``doc`` is an ATIF trajectory: its ``schema_version`` names ATIF, or (a producer that
    left the version out) it has an ``agent`` object and steps shaped ``{step_id, source}``."""
    if not isinstance(doc, dict):
        return False
    version = doc.get("schema_version")
    if isinstance(version, str) and version.strip().upper().startswith("ATIF"):
        return True
    steps = doc.get("steps")
    return (
        isinstance(doc.get("agent"), dict)
        and isinstance(steps, list)
        and bool(steps)
        and all(isinstance(s, dict) and "step_id" in s and "source" in s for s in steps[:3])
    )


def _items(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _text(value: Any) -> str:
    """The text of a ``message`` / ``content``: a string, or the ``text`` parts of a v1.6+
    ContentPart list (image and audio parts carry no text)."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(
            p["text"] for p in value if isinstance(p, dict) and isinstance(p.get("text"), str)
        )
    if value is None:
        return ""
    return json.dumps(value, default=str)


def _result_content(raw: Any) -> tuple[Any, str]:
    """``(content, text)`` of an observation result: the tool's result (parsed when it is
    serialized JSON, so a ``failure_when`` pointer reads it like any other source) and its text."""
    if raw is None:
        return None, ""
    text = _text(raw)
    if isinstance(raw, dict):
        return tool_result_content(raw)[0], text
    return tool_result_content(text)[0], text


def _integer(value: Any) -> int | None:
    """An integer field (an exit code, a token count); ``bool`` is not one, a digit string is."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and re.fullmatch(r"\s*-?\d+\s*", value):
        return int(value)
    return None


def _result_status(
    result: dict[str, Any], content: Any, text: str
) -> tuple[ResultStatus, str | None]:
    """``(status, error)`` from the structured signals ATIF producers record (module docstring)."""
    extra = result.get("extra") if isinstance(result.get("extra"), dict) else {}
    status = extra.get("status")
    status = status.strip().lower() if isinstance(status, str) else None
    if any(extra.get(key) is True for key in _ERROR_FLAGS) or _ERROR_MARKER.search(text):
        return ResultStatus.ERROR, None  # the tool's own message is the result content
    if status in _ERROR_STATUSES:
        return ResultStatus.ERROR, None
    error = content_error(content)
    if error is not None:
        return ResultStatus.ERROR, error
    if any(extra.get(key) is False for key in _ERROR_FLAGS) or status in _OK_STATUSES:
        return ResultStatus.OK, None
    if any(_integer(extra.get(key)) == 0 for key in _EXIT_CODE_KEYS):
        return ResultStatus.OK, None
    return ResultStatus.UNKNOWN, None


def _tool_schemas(definitions: Any) -> dict[str, dict[str, Any]]:
    """``{tool name: argument JSON Schema}`` from ``agent.tool_definitions`` (OpenAI format)."""
    schemas: dict[str, dict[str, Any]] = {}
    for definition in _items(definitions):
        if not isinstance(definition, dict):
            continue
        fn = definition.get("function")
        if not isinstance(fn, dict):
            fn = definition
        name, params = fn.get("name"), fn.get("parameters")
        if isinstance(name, str) and name and isinstance(params, dict):
            schemas.setdefault(name, params)
    return schemas


def _step_meta(step: dict[str, Any], default_model: Any) -> StepMeta | None:
    """The model and token counts of one agent turn, when the trajectory records them."""
    metrics = step.get("metrics") if isinstance(step.get("metrics"), dict) else {}
    model = step.get("model_name") or default_model
    meta = StepMeta(
        model=model if isinstance(model, str) else None,
        tokens_in=_integer(metrics.get("prompt_tokens")),
        tokens_out=_integer(metrics.get("completion_tokens")),
    )
    return meta if meta.to_dict() else None


def _observation_steps(
    step: dict[str, Any], *, copied: bool, call_ids: list[str]
) -> list[Step]:
    """A step's observation: a :class:`ToolResult` per result answering one of ``call_ids`` (the
    step's tool calls), a system :class:`Message` for anything else the agent was shown.

    Some producers record one observation for a whole turn rather than one per call — Harbor's
    Terminus sends a batch of keystrokes and records the terminal screen after them, with no
    ``source_call_id``. When a turn's calls went unanswered and exactly one such result is there,
    it is what the agent saw after the batch: it is paired with the last unanswered call (exact for
    a one-call turn); the earlier calls get no result of their own, because the agent saw none.
    """
    observation = step.get("observation")
    results = observation.get("results") if isinstance(observation, dict) else None
    results = [r for r in _items(results) if isinstance(r, dict)]
    answered = {str(r["source_call_id"]) for r in results if r.get("source_call_id")}
    unanswered = [c for c in call_ids if c not in answered]
    loose = [r for r in results if not r.get("source_call_id")]
    joint = loose[0] if len(loose) == 1 and unanswered and not copied else None
    out: list[Step] = []
    for result in results:
        content, text = _result_content(result.get("content"))
        call_id = unanswered[-1] if result is joint else result.get("source_call_id")
        if call_id not in (None, "") and not copied:
            status, error = _result_status(result, content, text)
            out.append(
                ToolResult(call_id=str(call_id), content=content, status=status, error=error)
            )
        elif text:
            out.append(Message(Role.SYSTEM, text))
    return out


def from_atif_trajectory(trajectory: Any, *, run_id: str | None = None) -> Trace:
    """Normalize one ATIF trajectory into a canonical :class:`Trace` (its own steps; embedded
    subagents are separate runs — see :func:`from_atif_trajectories`)."""
    if not isinstance(trajectory, dict):
        raise TypeError(
            "from_atif_trajectory expects an ATIF trajectory object, "
            f"got {type(trajectory).__name__}"
        )
    raw_steps = trajectory.get("steps")
    if not isinstance(raw_steps, list):
        raise ValueError("not an ATIF trajectory: it has no 'steps' list")
    agent = trajectory.get("agent") if isinstance(trajectory.get("agent"), dict) else {}
    schemas = _tool_schemas(agent.get("tool_definitions"))

    steps: list[Step] = []
    final = None
    for position, raw in enumerate(raw_steps):
        if not isinstance(raw, dict):
            continue
        source = str(raw.get("source") or "").lower()
        copied = raw.get("is_copied_context") is True
        text = _text(raw.get("message"))
        call_ids: list[str] = []
        if source == "agent":
            first = len(steps)
            reasoning = raw.get("reasoning_content")
            if isinstance(reasoning, str) and reasoning.strip():
                steps.append(Message(Role.ASSISTANT, reasoning))
            if text:
                steps.append(Message(Role.ASSISTANT, text))
                if not copied:
                    final = text
            if not copied:
                for n, call in enumerate(_items(raw.get("tool_calls"))):
                    if not isinstance(call, dict):
                        continue
                    name = str(call.get("function_name") or "")
                    args = model_call_args(call.get("arguments"))
                    call_id = str(call.get("tool_call_id") or f"atif-{position}-{n}")
                    call_ids.append(call_id)
                    steps.append(
                        ToolCall(
                            call_id=call_id,
                            name=name,
                            args=args.args,
                            raw_text=args.raw_text,
                            schema=schemas.get(name),
                        )
                    )
            if len(steps) > first:
                steps[first].meta = _step_meta(raw, agent.get("model_name"))
        elif source in ("user", "system") and text:
            steps.append(Message(Role.parse(source), text))
        steps.extend(_observation_steps(raw, copied=copied, call_ids=call_ids))

    resolved = run_id or trajectory.get("trajectory_id") or trajectory.get("session_id")
    return Trace(run_id=str(resolved or "atif-run"), steps=steps, final=final)


def from_atif_trajectories(trajectory: Any, *, run_id: str | None = None) -> list[Trace]:
    """A trajectory and every subagent embedded in it (``subagent_trajectories``), one
    :class:`Trace` each — a subagent's run is ``"<parent run id>/<its trajectory_id>"``."""
    root = from_atif_trajectory(trajectory, run_id=run_id)
    traces = [root]
    pending = [(trajectory, root.run_id)]
    while pending:  # iterative, so a deeply nested file cannot exhaust the stack
        parent, parent_id = pending.pop(0)
        for i, sub in enumerate(_items(parent.get("subagent_trajectories"))):
            if not isinstance(sub, dict) or not isinstance(sub.get("steps"), list):
                continue
            label = sub.get("trajectory_id") or sub.get("session_id") or f"subagent-{i + 1}"
            trace = from_atif_trajectory(sub, run_id=f"{parent_id}/{label}")
            traces.append(trace)
            pending.append((sub, trace.run_id))
    return traces


def atif_tools_to_registry(trajectory: Any) -> ToolRegistry:
    """A :class:`ToolRegistry` from the trajectory's ``agent.tool_definitions`` — the tools the
    agent was actually given — for when those definitions should be the contract R1 checks."""
    agent = trajectory.get("agent") if isinstance(trajectory, dict) else None
    definitions = agent.get("tool_definitions") if isinstance(agent, dict) else None
    return openai_tools_to_registry(definitions if isinstance(definitions, list) else [])
