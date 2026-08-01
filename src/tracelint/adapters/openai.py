"""OpenAI chat-completions adapter (spec §II.4).

Maps an OpenAI-style message list into a canonical :class:`Trace`:

- ``system`` / ``user`` / ``assistant`` text  → :class:`Message`
- an assistant message's ``tool_calls[]``      → :class:`ToolCall` (one per entry)
- a ``role: "tool"`` message                   → :class:`ToolResult` (paired by ``tool_call_id``)

The load-bearing detail (learning-doc 02 §3): OpenAI encodes tool-call ``arguments`` as a
**JSON-encoded string**, so the adapter ``json.loads`` it into ``ToolCall.args`` — the value R1
validates — while preserving the original string in ``raw_text`` for evidence when parsing fails.

The adapter stays **faithful, not clever**: it carries only structured error signals that are
explicitly present on a tool message (``status`` / ``error`` / ``http_status``). It does not
guess error-ness from free-form content — that heuristic belongs to R2a (Phase 2), which tiers it
as a candidate precisely because it is not deterministic.
"""

from __future__ import annotations

import json
from typing import Any

from tracelint.tools import ToolMetadata, ToolRegistry, ToolSpec
from tracelint.trace import Message, ResultStatus, Role, Step, ToolCall, ToolResult, Trace


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


def from_openai_messages(
    messages: list[dict[str, Any]],
    *,
    run_id: str = "openai-run",
    final: Any = None,
) -> Trace:
    """Normalize an OpenAI chat message list into a canonical :class:`Trace`."""
    steps: list[Step] = []
    for msg in messages:
        role = msg.get("role")
        if role == "tool":
            steps.append(
                ToolResult(
                    call_id=str(msg.get("tool_call_id", "")),
                    content=msg.get("content"),
                    status=_result_status(msg),
                    error=msg.get("error"),
                    http_status=msg.get("http_status"),
                )
            )
            continue

        content = msg.get("content")
        if role == "assistant":
            if content:
                steps.append(Message(Role.ASSISTANT, str(content)))
            for tc in msg.get("tool_calls") or []:
                fn = tc.get("function", tc)
                args, raw_text = _parse_arguments(fn.get("arguments"))
                steps.append(
                    ToolCall(
                        call_id=str(tc.get("id", "")),
                        name=fn.get("name", ""),
                        args=args,
                        raw_text=raw_text,
                    )
                )
        elif role in ("user", "system"):
            steps.append(Message(Role.parse(role), str(content or "")))
        # Unknown roles are skipped; a stricter adapter could suppress instead.

    if final is None:
        # Default the final answer to the last assistant text turn, if any.
        for msg in reversed(messages):
            if msg.get("role") == "assistant" and msg.get("content"):
                final = str(msg["content"])
                break
    return Trace(run_id=run_id, steps=steps, final=final)


def openai_tools_to_registry(tools: list[dict[str, Any]]) -> ToolRegistry:
    """Convert OpenAI tool definitions into a :class:`ToolRegistry`.

    Reads ``function.parameters`` as the argument schema so the *same* tool definitions that
    configure the agent also feed the linter. An optional ``function.metadata`` object supplies
    behavioural hints (``idempotent`` / ``polling`` / ...) for the later loop/error rules.
    """
    registry = ToolRegistry()
    for tool in tools:
        fn = tool.get("function", tool)
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
