"""Message-list reader — the OpenAI chat shape and its common real-world variants (spec §II.4).

Maps a message list into a canonical :class:`Trace`:

- ``system`` / ``user`` / ``assistant`` text  → :class:`Message`
- an assistant message's ``tool_calls[]``      → :class:`ToolCall` (one per entry)
- a ``role: "tool"`` message                   → :class:`ToolResult` (paired by ``tool_call_id``)

The load-bearing detail (learning-doc 02 §3): OpenAI encodes tool-call ``arguments`` as a
**JSON-encoded string**, so the adapter ``json.loads`` it into ``ToolCall.args`` — the value R1
validates — while preserving the original string in ``raw_text`` for evidence when parsing fails.

**Real message lists vary in three structural ways** (from surveying real agent-trace datasets),
all handled here without a bespoke adapter — the variation is field names and encoding, not shape:

- the **role key**: ``role`` (OpenAI) or ``from`` (ShareGPT — ``human``/``gpt`` aliased to
  ``user``/``assistant``);
- the **content key**: ``content`` (OpenAI) or ``value`` (ShareGPT) or ``text`` (some trajectory
  dumps);
- **content as typed blocks**: a string, or the Anthropic/newer-OpenAI list form
  ``[{"type": "text", "text": ...}]`` — flattened to text for messages, while a *structured*
  tool-result payload (a dict, or a data list) is kept intact so R2/``failure_when`` can read it.

The adapter stays **faithful, not clever**: it carries only structured error signals explicitly
present on a tool message (``status`` / ``error`` / ``http_status``); it never guesses error-ness
from free-form content (that heuristic is R2a's, tiered as a candidate). Tool calls are read only
from a *structured* ``tool_calls`` array — calls a format smuggles inside assistant text (e.g.
ShareGPT ``<tool_call>`` tags) are not parsed here.
"""

from __future__ import annotations

import json
from typing import Any

from tracelint.tools import ToolMetadata, ToolRegistry, ToolSpec
from tracelint.trace import Message, ResultStatus, Role, Step, ToolCall, ToolResult, Trace

# ShareGPT / alternate role names → the canonical role.
_ROLE_ALIASES = {
    "human": "user",
    "user": "user",
    "gpt": "assistant",
    "assistant": "assistant",
    "model": "assistant",
    "ai": "assistant",
    "system": "system",
    "tool": "tool",
    "observation": "tool",
    "function": "tool",
    "function_response": "tool",
    "tool_response": "tool",
}


def _role_of(msg: dict[str, Any]) -> str | None:
    """Canonical role from ``role`` (OpenAI) or ``from`` (ShareGPT), aliased to user/assistant."""
    raw = msg.get("role") or _ROLE_ALIASES.get(str(msg.get("from") or "").lower())
    return str(raw).lower() if raw else None


def _is_block_list(value: Any) -> bool:
    """True for a typed-block content list (``[{"type":"text","text":...}]`` / strings) — not a
    structured data list, which must be preserved rather than flattened to text."""
    return isinstance(value, list) and len(value) > 0 and all(
        isinstance(b, str)
        or (isinstance(b, dict) and ("text" in b or str(b.get("type", "")).endswith("text")))
        for b in value
    )


def _flatten_blocks(blocks: list[Any]) -> str:
    """Join the text out of a typed-block content list."""
    parts: list[str] = []
    for b in blocks:
        if isinstance(b, str):
            parts.append(b)
        elif isinstance(b, dict):
            for key in ("text", "content", "value"):
                if isinstance(b.get(key), str):
                    parts.append(b[key])
                    break
    return "\n".join(p for p in parts if p)


def _message_text(msg: dict[str, Any]) -> str:
    """Message text across ``content`` / ``value`` / ``text``, flattening typed-block content."""
    for key in ("content", "value", "text"):
        if key in msg:
            v = msg[key]
            if isinstance(v, str):
                return v
            if _is_block_list(v):
                return _flatten_blocks(v)
            return "" if v is None else str(v)
    return ""


def _result_content(msg: dict[str, Any]) -> Any:
    """Tool-result content: flatten typed blocks to text, but keep a structured dict/data list."""
    for key in ("content", "value", "output", "text"):
        if key in msg:
            v = msg[key]
            return _flatten_blocks(v) if _is_block_list(v) else v
    return None


def _parse_arguments(raw: Any) -> tuple[dict[str, Any], str | None]:
    """Return ``(args, raw_text)``. ``args`` is ``{}`` and ``raw_text`` kept if parsing fails."""
    if isinstance(raw, dict):
        return raw, None
    if raw is None or raw == "":
        return {}, None
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return {}, raw
        return (parsed, raw) if isinstance(parsed, dict) else ({}, raw)
    return {}, str(raw)


def _result_status(msg: dict[str, Any]) -> ResultStatus:
    if "status" in msg:
        return ResultStatus.parse(msg.get("status"))
    if msg.get("error") is not None:
        return ResultStatus.ERROR
    http = msg.get("http_status")
    if isinstance(http, int) and http >= 400:
        return ResultStatus.ERROR
    return ResultStatus.UNKNOWN


def _coerce_messages(messages: Any) -> list[dict[str, Any]]:
    """Reduce the input to a list of message dicts, or raise a clear error.

    Accepts the message list itself, or a common wrapper (``{"messages": [...]}`` / a ShareGPT
    ``{"conversations": [...]}`` record). A non-list, non-wrapping input is rejected with a
    ``TypeError`` rather than being iterated into an obscure crash; individual non-dict items
    inside the list are dropped (they are not messages), keeping a real export with stray entries
    readable instead of fatal.
    """
    if isinstance(messages, dict):
        for key in ("messages", "conversations", "conversation"):
            inner = messages.get(key)
            if isinstance(inner, list):
                messages = inner
                break
        else:
            raise TypeError(
                "from_openai_messages expects a list of messages "
                "(or a dict with a 'messages'/'conversations' list)"
            )
    if not isinstance(messages, list):
        raise TypeError(
            f"from_openai_messages expects a list of messages, got {type(messages).__name__}"
        )
    return [m for m in messages if isinstance(m, dict)]


def from_openai_messages(
    messages: list[dict[str, Any]],
    *,
    run_id: str = "openai-run",
    final: Any = None,
) -> Trace:
    """Normalize a message list (OpenAI or a common variant) into a canonical :class:`Trace`."""
    messages = _coerce_messages(messages)
    steps: list[Step] = []
    for msg in messages:
        role = _role_of(msg)
        if role == "tool":
            steps.append(
                ToolResult(
                    call_id=str(msg.get("tool_call_id", "")),
                    content=_result_content(msg),
                    status=_result_status(msg),
                    error=msg.get("error"),
                    http_status=msg.get("http_status"),
                )
            )
            continue

        if role == "assistant":
            text = _message_text(msg)
            if text:
                steps.append(Message(Role.ASSISTANT, text))
            tool_calls = msg.get("tool_calls")
            for tc in tool_calls if isinstance(tool_calls, list) else []:
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function")
                if not isinstance(fn, dict):
                    fn = tc
                args, raw_text = _parse_arguments(fn.get("arguments"))
                steps.append(
                    ToolCall(
                        call_id=str(tc.get("id", "")),
                        name=str(fn.get("name") or ""),
                        args=args,
                        raw_text=raw_text,
                    )
                )
        elif role in ("user", "system"):
            steps.append(Message(Role.parse(role), _message_text(msg)))
        # Unknown roles are skipped; a stricter adapter could suppress instead.

    if final is None:
        # Default the final answer to the last assistant text turn, if any.
        for msg in reversed(messages):
            if _role_of(msg) == "assistant":
                text = _message_text(msg)
                if text:
                    final = text
                    break
    return Trace(run_id=run_id, steps=steps, final=final)


def openai_tools_to_registry(tools: list[dict[str, Any]]) -> ToolRegistry:
    """Convert OpenAI tool definitions into a :class:`ToolRegistry`.

    Reads ``function.parameters`` as the argument schema so the *same* tool definitions that
    configure the agent also feed the linter. An optional ``function.metadata`` object supplies
    behavioural hints (``idempotent`` / ``polling`` / ...) for the later loop/error rules.
    """
    if not isinstance(tools, list):
        raise TypeError(
            f"openai_tools_to_registry expects a list of tool definitions, "
            f"got {type(tools).__name__}"
        )
    registry = ToolRegistry()
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        fn = tool.get("function")
        if not isinstance(fn, dict):
            fn = tool
        name = fn.get("name")
        if not name:
            continue
        registry.add(
            ToolSpec(
                name=name,
                schema=fn.get("parameters"),
                metadata=ToolMetadata.from_dict(fn.get("metadata")),
                schema_version=fn.get("schema_version"),
            )
        )
    return registry
