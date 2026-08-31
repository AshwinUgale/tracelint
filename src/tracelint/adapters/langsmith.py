"""LangSmith adapter — normalize a LangSmith run tree into the canonical schema.

LangSmith records a trace as a root run with nested ``child_runs``. Tool runs carry
their call arguments in ``inputs`` and their result in ``outputs``; LLM/chat runs can
carry assistant text in their outputs. The adapter keeps that mapping deliberately
small: it only treats ``run_type == "tool"`` as a tool call and leaves missing or
unclassifiable result status as ``unknown`` so downstream rules suppress rather than
pretend the trace is complete.
"""

from __future__ import annotations

import json
from typing import Any

from tracelint.trace import Message, ResultStatus, Role, Step, ToolCall, ToolResult, Trace


def _as_dict(run: Any) -> dict[str, Any]:
    if isinstance(run, dict):
        return run
    for attr in ("model_dump", "dict"):
        fn = getattr(run, attr, None)
        if callable(fn):
            result = fn()
            if isinstance(result, dict):
                return result
    raise TypeError(
        "from_langsmith_run expects a LangSmith run dict or an object exposing "
        ".model_dump()/.dict()"
    )


def _get(data: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in data:
            return data[key]
    return None


def _run_type(run: dict[str, Any]) -> str:
    return str(_get(run, "run_type", "runType") or "").lower()


def _unwrap_args(raw: dict[str, Any]) -> dict[str, Any]:
    if set(raw.keys()) <= {"args", "kwargs"}:
        kwargs = raw.get("kwargs")
        if isinstance(kwargs, dict) and kwargs:
            return dict(kwargs)
        args = raw.get("args")
        if isinstance(args, list) and len(args) == 1 and isinstance(args[0], dict):
            return dict(args[0])
        if isinstance(args, list) and args:
            # Positional-only args that don't flatten to a dict: preserve them under
            # ``args`` rather than dropping the call's arguments to ``{}`` (which would
            # make R1 false-positive a missing required field).
            return {"args": list(args)}
        if isinstance(kwargs, dict):
            return dict(kwargs)
    if set(raw.keys()) == {"input"} and isinstance(raw["input"], dict):
        return dict(raw["input"])
    return dict(raw)


def _tool_args(raw: Any) -> tuple[dict[str, Any], str | None]:
    if raw is None:
        return {}, None
    if isinstance(raw, dict):
        return _unwrap_args(raw), None
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return {"input": raw}, raw
        return (
            (_unwrap_args(parsed), None) if isinstance(parsed, dict) else ({"input": parsed}, raw)
        )
    return {"input": raw}, str(raw)


def _status_from_text(value: Any) -> ResultStatus:
    text = str(value or "").lower()
    if text in {"ok", "success", "succeeded", "complete", "completed"}:
        return ResultStatus.OK
    if text in {"error", "errored", "failed", "failure"}:
        return ResultStatus.ERROR
    return ResultStatus.UNKNOWN


def _result_signals(run: dict[str, Any]) -> tuple[ResultStatus, str | None, int | None]:
    outputs = _get(run, "outputs", "output")
    error = _get(run, "error", "error_message", "errorMessage")
    http: int | None = None
    if isinstance(outputs, dict):
        if error is None and outputs.get("error") is not None:
            error = outputs["error"]
        candidate_http = _get(outputs, "http_status", "httpStatus", "status_code", "statusCode")
        if isinstance(candidate_http, int):
            http = candidate_http
        status = _status_from_text(outputs.get("status"))
        if error is not None:
            return ResultStatus.ERROR, str(error), http
        if isinstance(http, int) and http >= 400:
            return ResultStatus.ERROR, None, http
        if status is not ResultStatus.UNKNOWN:
            return status, None, http

    if error is not None:
        return ResultStatus.ERROR, str(error), http
    run_http = _get(run, "http_status", "httpStatus", "status_code", "statusCode")
    if isinstance(run_http, int):
        # A numeric HTTP status at the run level is an error signal too (>= 400),
        # mirroring the ``outputs`` branch; ``_status_from_text`` only reads words.
        http = run_http
        if run_http >= 400:
            return ResultStatus.ERROR, None, http
    status = _status_from_text(_get(run, "status", "status_code", "statusCode"))
    return status, None, http


def _first_text(value: Any) -> str | None:
    if isinstance(value, str):
        return value or None
    if isinstance(value, dict):
        for key in ("content", "text", "output", "input"):
            found = _first_text(value.get(key))
            if found:
                return found
        message = value.get("message")
        if isinstance(message, dict):
            found = _first_text(message)
            if found:
                return found
        generations = value.get("generations")
        if isinstance(generations, list):
            found = _first_text(generations)
            if found:
                return found
    if isinstance(value, list):
        for item in value:
            found = _first_text(item)
            if found:
                return found
    return None


def _seed_messages(inputs: Any) -> list[Step]:
    text = _first_text(inputs)
    return [Message(Role.USER, text)] if text else []


def _order_key(run: dict[str, Any], idx: int) -> tuple[int, str, int]:
    """A total order over sibling runs that is correct for each field's own type.

    ``dotted_order`` and ISO ``start_time`` sort correctly as strings, but
    ``execution_order`` is an integer: sorting it as a string puts ``"10"`` before
    ``"2"``. Zero-pad the numeric case so lexical order matches numeric order, and
    keep the original index as the final tie-breaker (stable).
    """
    dotted = _get(run, "dotted_order", "dottedOrder")
    if dotted is not None:
        return (0, str(dotted), idx)
    start = _get(run, "start_time", "startTime")
    if start is not None:
        return (1, str(start), idx)
    execn = _get(run, "execution_order", "executionOrder")
    if isinstance(execn, bool):  # bool is an int subclass; not an ordering signal
        execn = None
    if isinstance(execn, int):
        return (2, f"{execn:020d}", idx)
    if execn is not None:
        return (2, str(execn), idx)
    return (3, "", idx)


def _children(run: dict[str, Any]) -> list[dict[str, Any]]:
    raw = _get(run, "child_runs", "childRuns") or []
    children = [_as_dict(child) for child in raw]
    keyed = sorted(enumerate(children), key=lambda item: _order_key(item[1], item[0]))
    return [child for _, child in keyed]


def _walk_children(run: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for child in _children(run):
        out.append(child)
        out.extend(_walk_children(child))
    return out


def from_langsmith_run(run: Any, *, run_id: str | None = None, final: Any = None) -> Trace:
    """Normalize a LangSmith run tree into a canonical :class:`Trace`."""
    data = _as_dict(run)
    steps: list[Step] = list(_seed_messages(_get(data, "inputs", "input")))
    runs = ([data] if _run_type(data) in {"tool", "llm", "chat_model"} else []) + _walk_children(
        data
    )

    for child in runs:
        kind = _run_type(child)
        if kind == "tool":
            call_id = str(_get(child, "id", "run_id", "runId") or f"run-{len(steps)}")
            args, raw_text = _tool_args(_get(child, "inputs", "input"))
            steps.append(
                ToolCall(
                    call_id=call_id, name=str(child.get("name") or ""), args=args, raw_text=raw_text
                )
            )
            status, error, http = _result_signals(child)
            steps.append(
                ToolResult(
                    call_id=call_id,
                    content=_get(child, "outputs", "output"),
                    status=status,
                    error=error,
                    http_status=http,
                )
            )
        elif kind in {"llm", "chat_model"}:
            text = _first_text(_get(child, "outputs", "output"))
            if text:
                steps.append(Message(Role.ASSISTANT, text))

    resolved_final = final if final is not None else _get(data, "outputs", "output")
    resolved_run_id = run_id or str(_get(data, "id", "run_id", "runId", "name") or "langsmith-run")
    return Trace(run_id=resolved_run_id, steps=steps, final=resolved_final)
