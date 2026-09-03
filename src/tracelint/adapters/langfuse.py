"""Langfuse adapter — normalize a Langfuse trace into a canonical :class:`Trace` (spec §II.4).

Langfuse stores a run as a **trace** with a list of nested **observations**, each typed
``generation`` (an LLM call), ``span`` / ``tool`` / ``retriever`` / ``agent`` (a step), or
``event``. This adapter maps that structure into tracelint's small vocabulary so the rules
never see Langfuse-specific shapes.

**How a tool call is recognized** (highest- to lowest-fidelity; the first that matches wins):

1. ``type == "tool"`` — Langfuse v4's semantic observation type for a tool execution.
2. ``name`` is in the caller-supplied ``tool_names`` — the reliable path for older span-based
   instrumentation, where each tool runs inside a span named after the tool.
3. A metadata hint — ``metadata.tracelint == "tool"`` / ``metadata.kind == "tool"`` /
   OpenInference ``openinference.span.kind == "TOOL"`` / a ``gen_ai.tool.name`` attribute.
4. Failing all of the above, OpenAI-style ``tool_calls[]`` embedded in a *generation's* output
   (only when the trace has **no** tool/span-based tool observations, so a call is never counted
   twice).

A recognized tool observation yields a paired :class:`ToolCall` + :class:`ToolResult` sharing
the observation ``id`` as their ``call_id`` — its ``input`` is the arguments, its ``output`` the
result. A generation yields an assistant :class:`Message` for any text.

**Faithful, not clever** (the same discipline as the OpenAI adapter): an error is only recorded
from a *structured* signal — ``level == "ERROR"``, or a ``status`` / ``error`` / ``http_status``
field in the output. Anything else stays :class:`ResultStatus.UNKNOWN`, leaving R2a to decide
heuristically at the candidate tier. The adapter never guesses error-ness from free text.

**Fidelity caveats (deep-design Trap 1).** The linter is only as good as the trace. Langfuse
instrumentation varies: if tool executions aren't captured as ``tool``/named-span observations
(or their names aren't passed as ``tool_names``), tool calls won't be seen; if arguments/results
aren't recorded, arg-level rules (R1/R3) suppress. Tool **schemas** are almost never present in a
trace, so bring them as a separate ``tools.json`` (:class:`~tracelint.tools.ToolRegistry`) — that
is what R1 validates against and what upgrades R3 to a hard defect. Trace-fetch shape is taken
from the Langfuse SDK/API (``trace`` with ``observations``); validate on a live trace before
relying on any specific field.
"""

from __future__ import annotations

import json
from typing import Any

from tracelint.trace import (
    Message,
    ResultStatus,
    Role,
    SourceRef,
    Step,
    ToolCall,
    ToolResult,
    Trace,
)

_TOOL_META_KEYS = ("tracelint", "kind")


def _as_dict(trace: Any) -> dict[str, Any]:
    """Accept a plain dict (raw API JSON) or an SDK object (``.model_dump()`` / ``.dict()``)."""
    if isinstance(trace, dict):
        return trace
    for attr in ("model_dump", "dict"):
        fn = getattr(trace, attr, None)
        if callable(fn):
            result = fn()
            if isinstance(result, dict):
                return result
    raise TypeError(
        "from_langfuse_trace expects a Langfuse trace dict or an object exposing "
        ".model_dump()/.dict()"
    )


def _unwrap_kwargs(d: dict[str, Any]) -> dict[str, Any]:
    """Langfuse ``@observe`` records a wrapped function's inputs as ``{"args": [...],
    "kwargs": {...}}``. The tool's real arguments live in ``kwargs`` (keyword call) or, when the
    tool is called positionally with a single dict (``func({...})``), in ``args[0]`` — verified
    against a real v4 trace where the arg dict landed in ``args[0]`` with an empty ``kwargs``.
    Bounded to that exact shape so nothing else is second-guessed.
    """
    if set(d.keys()) <= {"args", "kwargs"}:
        kwargs = d.get("kwargs")
        if isinstance(kwargs, dict) and kwargs:
            return dict(kwargs)
        args = d.get("args")
        if isinstance(args, list) and len(args) == 1 and isinstance(args[0], dict):
            return dict(args[0])
        if isinstance(kwargs, dict):
            return dict(kwargs)
    return d


def _parse_tool_input(raw: Any) -> tuple[dict[str, Any], str | None]:
    """Return ``(args, raw_text)`` for a tool observation's ``input`` (dict / JSON string)."""
    if raw is None:
        return {}, None
    if isinstance(raw, dict):
        return _unwrap_kwargs(raw), None
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return {}, raw
        return (_unwrap_kwargs(parsed), None) if isinstance(parsed, dict) else ({}, raw)
    return {}, str(raw)


def _parse_openai_arguments(raw: Any) -> tuple[dict[str, Any], str | None]:
    """Parse an OpenAI tool_call ``arguments`` (a JSON-encoded string) — same rule as the
    OpenAI adapter, kept local so the Langfuse adapter has no cross-adapter dependency."""
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


def _is_tool_observation(obs: dict[str, Any], known_names: set[str]) -> bool:
    if str(obs.get("type") or "").lower() == "tool":
        return True
    name = obs.get("name")
    if name and name in known_names:
        return True
    meta = obs.get("metadata")
    if isinstance(meta, dict):
        for key in _TOOL_META_KEYS:
            if str(meta.get(key) or "").lower() == "tool":
                return True
        if str(meta.get("openinference.span.kind") or "").upper() == "TOOL":
            return True
        if meta.get("gen_ai.tool.name") or meta.get("tool.name"):
            return True
    return False


def _result_signals(obs: dict[str, Any]) -> tuple[ResultStatus, str | None, int | None]:
    """Extract ``(status, error, http_status)`` from a tool obs — structured signals only."""
    level = str(obs.get("level") or "").upper()
    # The fetched SDK shape is snake_case (status_message); the public API is camelCase.
    status_message = obs.get("statusMessage") or obs.get("status_message")
    out = obs.get("output")
    error: str | None = None
    http: int | None = None
    status_field: str | None = None
    if isinstance(out, dict):
        if out.get("error") is not None:
            error = str(out.get("error"))
        candidate_http = out.get("http_status", out.get("status_code"))
        if isinstance(candidate_http, int):
            http = candidate_http
        if isinstance(out.get("status"), str):
            status_field = out["status"]

    if level == "ERROR":
        return ResultStatus.ERROR, error or (status_message or None), http
    if error is not None:
        return ResultStatus.ERROR, error, http
    if isinstance(http, int) and http >= 400:
        return ResultStatus.ERROR, status_message, http
    if status_field is not None:
        parsed = ResultStatus.parse(status_field)
        if parsed is not ResultStatus.UNKNOWN:
            return parsed, error, http
    return ResultStatus.UNKNOWN, error, http


def _generation_text(output: Any) -> str | None:
    if isinstance(output, str):
        return output or None
    if isinstance(output, dict):
        content = output.get("content")
        if isinstance(content, str):
            return content or None
        msg = output.get("message")
        if isinstance(msg, dict) and isinstance(msg.get("content"), str):
            return msg["content"] or None
    return None


def _tool_calls_from_generation(output: Any) -> list[tuple[str, str, dict[str, Any], str | None]]:
    """Extract OpenAI-style ``tool_calls`` from a generation's output (fallback path)."""
    messages: list[dict[str, Any]] = []
    if isinstance(output, dict):
        if "tool_calls" in output:
            messages = [output]
        elif isinstance(output.get("message"), dict):
            messages = [output["message"]]
        elif isinstance(output.get("choices"), list):
            messages = [c.get("message", {}) for c in output["choices"] if isinstance(c, dict)]
    elif isinstance(output, list):
        messages = [m for m in output if isinstance(m, dict)]

    calls: list[tuple[str, str, dict[str, Any], str | None]] = []
    for msg in messages:
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function", tc)
            args, raw_text = _parse_openai_arguments(fn.get("arguments"))
            calls.append((str(tc.get("id", "")), fn.get("name", ""), args, raw_text))
    return calls


def _seed_messages(trace_input: Any) -> list[Step]:
    """Turn a trace's top-level ``input`` into leading Message steps (str or message list)."""
    if isinstance(trace_input, str):
        return [Message(Role.USER, trace_input)] if trace_input else []
    if isinstance(trace_input, dict) and isinstance(trace_input.get("messages"), list):
        return _seed_messages(trace_input["messages"])
    # @observe root captures the entry function's call as {"args": [task], "kwargs": {...}};
    # surface the string task as the user turn so provenance (R3) sees what the user asked.
    if isinstance(trace_input, dict) and set(trace_input.keys()) <= {"args", "kwargs"}:
        values = list(trace_input.get("args") or [])
        kwargs = trace_input.get("kwargs")
        if isinstance(kwargs, dict):
            values += list(kwargs.values())
        return [Message(Role.USER, v) for v in values if isinstance(v, str) and v]
    steps: list[Step] = []
    if isinstance(trace_input, list):
        for m in trace_input:
            if not isinstance(m, dict):
                continue
            role, content = m.get("role"), m.get("content")
            if role in ("user", "system", "assistant") and isinstance(content, str) and content:
                steps.append(Message(Role.parse(role), content))
    return steps


def _ordered_observations(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Observations ordered by ``startTime`` (stable; original order when times are absent)."""
    obs = [o for o in (data.get("observations") or []) if isinstance(o, dict)]
    keyed = sorted(
        enumerate(obs),
        key=lambda t: (str(t[1].get("startTime") or t[1].get("start_time") or ""), t[0]),
    )
    return [o for _, o in keyed]


def from_langfuse_trace(
    trace: Any,
    *,
    tool_names: list[str] | set[str] | None = None,
    run_id: str | None = None,
    final: Any = None,
) -> Trace:
    """Normalize a Langfuse trace (dict or SDK object) into a canonical :class:`Trace`.

    ``tool_names`` names the observations to treat as tool executions when the trace does not use
    the v4 ``tool`` type or a metadata hint — typically the same tool names as your ``tools.json``.
    """
    data = _as_dict(trace)
    known = {str(t) for t in (tool_names or ())}
    observations = _ordered_observations(data)
    has_tool_obs = any(_is_tool_observation(o, known) for o in observations)
    src_trace_id = str(data["id"]) if data.get("id") else None

    def _src(obs_id: str | None) -> SourceRef:
        return SourceRef(provider="langfuse", trace_id=src_trace_id, observation_id=obs_id)

    steps: list[Step] = list(_seed_messages(data.get("input")))
    for obs in observations:
        obs_id = str(obs["id"]) if obs.get("id") else None
        if _is_tool_observation(obs, known):
            call_id = str(obs.get("id") or f"obs-{len(steps)}")
            args, raw_text = _parse_tool_input(obs.get("input"))
            steps.append(
                ToolCall(
                    call_id=call_id,
                    name=str(obs.get("name") or ""),
                    args=args,
                    raw_text=raw_text,
                    source=_src(obs_id),
                )
            )
            status, error, http = _result_signals(obs)
            steps.append(
                ToolResult(
                    call_id=call_id,
                    content=obs.get("output"),
                    status=status,
                    error=error,
                    http_status=http,
                    source=_src(obs_id),
                )
            )
            continue

        if str(obs.get("type") or "").lower() == "generation":
            text = _generation_text(obs.get("output"))
            if text:
                steps.append(Message(Role.ASSISTANT, text, source=_src(obs_id)))
            if not has_tool_obs:
                for call_id, name, args, raw_text in _tool_calls_from_generation(obs.get("output")):
                    steps.append(ToolCall(call_id=call_id, name=name, args=args, raw_text=raw_text))
        # Other observation types (span/retriever/chain/agent/event that aren't tools) are
        # skipped — faithful, not clever: we do not invent tool calls from arbitrary spans.

    if final is None:
        final = data.get("output")
        if final is None:
            for step in reversed(steps):
                if isinstance(step, Message) and step.role is Role.ASSISTANT and step.content:
                    final = step.content
                    break

    resolved_run_id = run_id or str(data.get("id") or data.get("name") or "langfuse-run")
    return Trace(run_id=resolved_run_id, steps=steps, final=final)


def observed_tool_names(trace: Any, *, tool_names: list[str] | set[str] | None = None) -> list[str]:
    """The tool-observation names tracelint would recognize in this trace — a convenience for
    discovering what to declare in ``tools.json``."""
    data = _as_dict(trace)
    known = {str(t) for t in (tool_names or ())}
    names = {
        str(o.get("name"))
        for o in (data.get("observations") or [])
        if isinstance(o, dict) and _is_tool_observation(o, known) and o.get("name")
    }
    return sorted(names)
