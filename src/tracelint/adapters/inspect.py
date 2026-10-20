"""Inspect reader — the chat-message model of Inspect AI, and so of Inspect Scout (spec §II.4).

Inspect (https://inspect.aisi.org.uk) records an agent run as a list of ``ChatMessage`` s, and
Inspect Scout reads every transcript it scans — Inspect eval logs, and the traces it imports from
Phoenix, LangSmith, Logfire, Weave, MLflow, Claude Code and Harbor — into that same model. One
reader therefore covers all of them. It takes the messages as Inspect's pydantic objects or as their
JSON form (``model_dump()``), so it needs no Inspect install.

Mapping into a canonical :class:`Trace`:

- a ``system`` / ``user`` message's text                         → :class:`Message`
- an ``assistant`` message's reasoning and text                  → assistant :class:`Message`
- each of its ``tool_calls[]``                                    → :class:`ToolCall`
- a ``tool`` message                                              → :class:`ToolResult`, paired by
  ``tool_call_id``
- a server-side tool use inside assistant content (``tool_use``: a provider's web search, MCP call
  or code execution, with its result in the same block)          → a :class:`ToolCall` and its
  :class:`ToolResult`

**Arguments.** Inspect parses the model's arguments itself and keeps the parsed object; when they
were not valid JSON it records ``parse_error`` (with the raw text) and an empty object. That raw
text is the model's own output, so it is kept as ``raw_text`` for R6. A server-side tool's arguments
are the provider's record of the call, not text the model emitted, so broken JSON there is never
proof of a malformed call.

**Result status.** A tool message's ``error`` (Inspect's ``ToolCallError``: a parsing failure, a
timeout, a denied approval, ...) is a structured failure, and so is the ``error`` of the
transcript's ``tool`` event for the same call: Scout's importers record a failed call there (Claude
Code's ``is_error``, a failed span) and leave the message's own field empty, so pass the
transcript's events too. A truthy ``error`` field in a JSON result is one as well, and so is the
line ``[error] tool reported failure`` that Harbor's Claude Code converter appends to a failed
result: Scout's ATIF importer keeps that line but not the flag behind it, and tracelint's own ATIF
reader treats it the same way, so a Harbor run gets one verdict whichever way it is read. Inspect
has no field that says a tool succeeded — a tool that prints its own error text returns normally —
so every other result is ``unknown``, and R2a's heuristics read it as a candidate, never a fact.

:func:`inspect_trace` also returns, for each step, the position of the message it came from, so an
integration can cite the exact message a finding is about (Inspect Scout's ``[M3]``); each step's
``source`` carries that message's ``id``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from typing import Any

from tracelint.adapters._common import (
    content_error,
    model_call_args,
    tool_input_args,
    tool_result_content,
)
from tracelint.adapters.atif import _ERROR_MARKER as _HARBOR_ERROR_MARKER
from tracelint.trace import (
    Message,
    ResultStatus,
    Role,
    SourceRef,
    Step,
    StepMeta,
    ToolCall,
    ToolResult,
    Trace,
)

PROVIDER = "inspect"

# Inspect's message for arguments it could not parse (``tool_parse_error_message``): the raw
# arguments sit between this header and the "Error details" trailer.
_PARSE_ERROR = re.compile(
    r"\AError parsing the following tool call arguments:\n\n(?P<raw>.*)\n\nError details: ",
    re.DOTALL,
)


def _get(obj: Any, name: str) -> Any:
    """A field of an Inspect object or of its JSON form."""
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _items(value: Any) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple)) else []


def _text(content: Any) -> str:
    """The text of a message's ``content``: a string, or the ``text`` parts of a content list
    (images, audio and documents carry no text)."""
    if isinstance(content, str):
        return content
    parts = [
        _get(part, "text")
        for part in _items(content)
        if _get(part, "type") == "text" and isinstance(_get(part, "text"), str)
    ]
    return "\n".join(p for p in parts if p)


def _error_text(error: Any) -> str | None:
    """A ``ToolCallError`` (or a bare string) as ``"<type>: <message>"``; ``None`` when absent."""
    if error is None or error == "":
        return None
    if isinstance(error, str):
        return error
    kind, message = _get(error, "type"), _get(error, "message")
    text = ": ".join(str(p) for p in (kind, message) if p)
    return text or json.dumps(error, default=str)


def _raw_arguments(parse_error: str) -> str:
    """The raw argument text inside Inspect's parse-error message (the message itself if it is not
    in Inspect's format). Either way it is not a JSON object, which is what R6 checks."""
    match = _PARSE_ERROR.match(parse_error)
    return match.group("raw") if match else parse_error


def _call_args(call: Any) -> tuple[dict[str, Any], str | None]:
    """``(args, raw_text)`` for a client-side tool call the model emitted."""
    parse_error = _get(call, "parse_error")
    if parse_error:
        return {}, _raw_arguments(str(parse_error))
    parsed = model_call_args(_get(call, "arguments"))
    return parsed.args, parsed.raw_text


def _result(call_id: str, raw: Any, error: Any) -> ToolResult:
    """A :class:`ToolResult` from a tool's output and its recorded error (structured signals
    only)."""
    text = raw if isinstance(raw, str) else _text(raw)
    content, _ = tool_result_content(text)
    message = _error_text(error)
    if message is None:
        message = content_error(content)
    failed = message is not None or bool(_HARBOR_ERROR_MARKER.search(text))
    status = ResultStatus.ERROR if failed else ResultStatus.UNKNOWN
    return ToolResult(call_id=call_id, content=content, status=status, error=message)


def _event_errors(events: Iterable[Any] | None) -> dict[str, Any]:
    """``{tool call id: error}`` from the transcript's ``tool`` events that record a failure."""
    errors: dict[str, Any] = {}
    for event in events or ():
        if _get(event, "event") != "tool" or not _get(event, "id"):
            continue
        error = _get(event, "error")
        if error is None and _get(event, "failed") is True:
            error = "the tool call failed"
        if error is not None:
            errors[str(_get(event, "id"))] = error
    return errors


def _server_tool_steps(block: Any) -> list[Step]:
    """A server-side tool use (``tool_use`` content): the call and the result recorded with it."""
    call_id = str(_get(block, "id") or "")
    args = tool_input_args(_get(block, "arguments"))
    call = ToolCall(
        call_id=call_id,
        name=str(_get(block, "name") or _get(block, "tool_type") or ""),
        args=args.args,
        raw_text=args.raw_text,
        args_unavailable=args.unavailable,
    )
    return [call, _result(call_id, _get(block, "result"), _get(block, "error"))]


def _assistant_steps(message: Any) -> list[Step]:
    """An assistant turn: its reasoning and text (in content order), server-side tool uses, then
    the client-side tool calls it requested."""
    steps: list[Step] = []
    content = _get(message, "content")
    text: list[str] = []

    def flush() -> None:
        joined = "\n".join(t for t in text if t)
        if joined:
            steps.append(Message(Role.ASSISTANT, joined))
        text.clear()

    for block in [content] if isinstance(content, str) else _items(content):
        kind = "text" if isinstance(block, str) else _get(block, "type")
        if kind == "text":
            text.append(block if isinstance(block, str) else str(_get(block, "text") or ""))
        elif kind == "reasoning":
            reasoning = _get(block, "reasoning")
            if not _get(block, "redacted") and isinstance(reasoning, str) and reasoning.strip():
                flush()
                steps.append(Message(Role.ASSISTANT, reasoning))
        elif kind == "tool_use":
            flush()
            steps.extend(_server_tool_steps(block))
    flush()

    for n, call in enumerate(_items(_get(message, "tool_calls"))):
        args, raw_text = _call_args(call)
        steps.append(
            ToolCall(
                call_id=str(_get(call, "id") or f"inspect-call-{n}"),
                name=str(_get(call, "function") or ""),
                args=args,
                raw_text=raw_text,
            )
        )
    model = _get(message, "model")
    if steps and isinstance(model, str) and model:
        steps[0].meta = StepMeta(model=model)
    return steps


def _unanswered_call(turn: list[ToolCall], answered: set[str], function: Any) -> str | None:
    """For a tool message recorded without ``tool_call_id`` (older logs): the first still-unanswered
    call of the latest assistant turn — Inspect runs a turn's calls in order and records their
    results in that order — naming ``function`` when the message records one."""
    for call in turn:
        if call.call_id not in answered and (not function or call.name == function):
            return call.call_id
    return None


def inspect_trace(
    messages: Iterable[Any],
    *,
    events: Iterable[Any] | None = None,
    run_id: str = "inspect-run",
) -> tuple[Trace, list[int]]:
    """Normalize Inspect chat messages into a :class:`Trace`, plus the position (in ``messages``)
    of the message each step came from. ``events`` (the transcript's events, or just its ``tool``
    events) supplies the failures an importer recorded only there."""
    if isinstance(messages, (str, bytes, dict)) or not isinstance(messages, Iterable):
        raise TypeError(
            f"inspect_trace expects a list of Inspect chat messages, got {type(messages).__name__}"
        )
    event_errors = _event_errors(events)
    steps: list[Step] = []
    positions: list[int] = []
    answered: set[str] = set()
    turn: list[ToolCall] = []  # the latest assistant turn's client-side calls
    final = None
    for position, message in enumerate(messages):
        role = str(_get(message, "role") or "").lower()
        new: list[Step]
        if role == "assistant":
            new = _assistant_steps(message)
            served = {s.call_id for s in new if isinstance(s, ToolResult)}  # server-side tool uses
            turn = [s for s in new if isinstance(s, ToolCall) and s.call_id not in served]
            text = _text(_get(message, "content"))
            if text:
                final = text
        elif role == "tool":
            call_id = _get(message, "tool_call_id")
            if not call_id:
                call_id = _unanswered_call(turn, answered, _get(message, "function"))
            call_id = str(call_id or "")
            answered.add(call_id)
            error = _get(message, "error") or event_errors.get(call_id)
            new = [_result(call_id, _get(message, "content"), error)]
        elif role in ("system", "user"):
            text = _text(_get(message, "content"))
            new = [Message(Role.parse(role), text)] if text else []
        else:
            new = []  # not a chat message
        message_id = _get(message, "id")
        for step in new:
            step.source = SourceRef(
                provider=PROVIDER, observation_id=str(message_id) if message_id else None
            )
        steps.extend(new)
        positions.extend([position] * len(new))
    return Trace(run_id=run_id, steps=steps, final=final), positions


def from_inspect_messages(
    messages: Iterable[Any],
    *,
    events: Iterable[Any] | None = None,
    run_id: str = "inspect-run",
) -> Trace:
    """Normalize Inspect chat messages (objects or their JSON form) into a :class:`Trace`;
    ``events`` as in :func:`inspect_trace`."""
    return inspect_trace(messages, events=events, run_id=run_id)[0]
