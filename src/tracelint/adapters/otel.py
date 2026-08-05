"""OpenTelemetry / OpenInference adapter — normalize OTel spans into a canonical Trace.

OpenTelemetry is the universal standard for agent tracing, and **OpenInference** (Arize) is the
semantic convention that gives OTel spans their AI-specific meaning. Anything instrumented with
OpenInference — Arize Phoenix, Langfuse-via-OTel, LangSmith export, OpenLLMetry, and research
datasets like TRAIL — emits the same span attributes, so one adapter reaches the whole ecosystem
instead of one vendor.

**What it reads** (per the OpenInference semantic conventions):

- A **TOOL** span (``openinference.span.kind == "TOOL"``) → a paired :class:`ToolCall` +
  :class:`ToolResult`. ``tool.name`` (or the span name) is the tool; ``input.value`` is the
  arguments; ``output.value`` is the result. An OTel span ``status_code == "ERROR"`` or an
  ``exception`` span event marks the result as a structured error (what R2a reads).
- An **LLM** span → an assistant :class:`Message` for any output text, and — only when the trace
  has no TOOL spans — the OpenAI-style ``tool_calls`` embedded in
  ``llm.output_messages.*.message.tool_calls.*`` (so a call is never counted twice).

**Two serializations are accepted** for a span's attributes: a *flat dict* with dotted keys
(Arize Phoenix / most JSON exports) and the raw *OTLP* attribute list (``[{"key","value":{...}}]``).
Both normalize to the same flat map before parsing.

**Faithful, not clever** (the same discipline as the other adapters): an error is recorded only
from a *structured* signal (OTel ERROR status, an exception event, or a status/error field in the
output) — never guessed from free text. Missing fields cause the relevant rules to *suppress*, not
to run on partial data.

*Targets the OpenInference spec; validate against a real Phoenix/TRAIL export before relying on a
specific field — real exports vary, as the Langfuse adapter's snake_case/positional-args quirks
showed.*
"""

from __future__ import annotations

import json
from typing import Any

from tracelint.trace import Message, ResultStatus, Role, Step, ToolCall, ToolResult, Trace

_SPAN_KIND = "openinference.span.kind"


def _otlp_value(v: Any) -> Any:
    """Unwrap an OTLP typed attribute value (``{"stringValue": ...}``) to a plain Python value."""
    if not isinstance(v, dict):
        return v
    for key in ("stringValue", "intValue", "doubleValue", "boolValue"):
        if key in v:
            return v[key]
    if "arrayValue" in v:
        vals = v["arrayValue"].get("values", [])
        return [_otlp_value(x) for x in vals]
    return v


def _attrs(span: dict[str, Any]) -> dict[str, Any]:
    """Return a span's attributes as a flat ``{dotted_key: value}`` map (flat-dict or OTLP list)."""
    raw = span.get("attributes", span.get("attribute", {}))
    if isinstance(raw, dict):
        return raw
    flat: dict[str, Any] = {}
    if isinstance(raw, list):  # OTLP: [{"key": "...", "value": {"stringValue": "..."}}, ...]
        for item in raw:
            if isinstance(item, dict) and "key" in item:
                flat[item["key"]] = _otlp_value(item.get("value"))
    return flat


def _get(span: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in span and span[key] is not None:
            return span[key]
        # dotted access into nested dicts (e.g. "context.span_id", "status.code")
        if "." in key:
            cur: Any = span
            for part in key.split("."):
                cur = cur.get(part) if isinstance(cur, dict) else None
                if cur is None:
                    break
            if cur is not None:
                return cur
    return None


def _span_id(span: dict[str, Any]) -> str:
    return str(_get(span, "span_id", "spanId", "id", "context.span_id") or "")


def _start_key(span: dict[str, Any]) -> str:
    return str(
        _get(span, "start_time", "startTime", "startTimeUnixNano", "start_time_unix_nano") or ""
    )


def _parse_value(raw: Any) -> tuple[Any, str | None]:
    """Parse an OpenInference ``*.value`` (often a JSON string). Return ``(value, raw_text)``."""
    if raw is None or isinstance(raw, (dict, list, int, float, bool)):
        return raw, None
    if isinstance(raw, str):
        try:
            return json.loads(raw), None
        except (json.JSONDecodeError, ValueError):
            return raw, raw
    return raw, None


def _args_from(raw: Any) -> tuple[dict[str, Any], str | None]:
    value, raw_text = _parse_value(raw)
    if isinstance(value, dict):
        return value, None
    return {}, raw_text if isinstance(raw_text, str) else (
        str(value) if value is not None else None
    )


def _is_error_span(span: dict[str, Any], attrs: dict[str, Any]) -> tuple[bool, str | None]:
    status = str(_get(span, "status_code", "statusCode", "status.code", "status") or "").upper()
    message = _get(span, "status_message", "statusMessage", "status.message")
    if status == "ERROR":
        return True, (str(message) if message else None)
    for event in span.get("events", []) or []:
        if isinstance(event, dict) and str(event.get("name", "")).lower() == "exception":
            ev_attrs = event.get("attributes", {})
            if isinstance(ev_attrs, list):
                ev_attrs = {i.get("key"): _otlp_value(i.get("value")) for i in ev_attrs}
            return True, str(ev_attrs.get("exception.message") or "exception")
    out = attrs.get("output.value")
    parsed, _ = _parse_value(out)
    if isinstance(parsed, dict):
        http = parsed.get("http_status", parsed.get("status_code"))
        if isinstance(http, int) and http >= 400:
            return True, str(parsed.get("detail") or message or "")
        if parsed.get("error") is not None:
            return True, str(parsed["error"])
    return False, None


def _collect_messages(attrs: dict[str, Any], prefix: str) -> list[dict[str, Any]]:
    """Rebuild indexed OpenInference message objects from dotted keys under ``prefix``.

    e.g. ``llm.output_messages.0.message.content`` /
    ``llm.output_messages.0.message.tool_calls.0.tool_call.function.name``.
    """
    messages: dict[int, dict[str, Any]] = {}
    for key, val in attrs.items():
        if not key.startswith(prefix + "."):
            continue
        rest = key[len(prefix) + 1 :]
        head, _, tail = rest.partition(".")
        if not head.isdigit():
            continue
        idx = int(head)
        msg = messages.setdefault(idx, {"role": None, "content": None, "tool_calls": {}})
        if tail == "message.role":
            msg["role"] = val
        elif tail == "message.content":
            msg["content"] = val
        elif tail.startswith("message.tool_calls."):
            tc_rest = tail[len("message.tool_calls.") :]
            tci, _, tc_field = tc_rest.partition(".")
            if tci.isdigit():
                tc = msg["tool_calls"].setdefault(int(tci), {})
                if tc_field == "tool_call.function.name":
                    tc["name"] = val
                elif tc_field == "tool_call.function.arguments":
                    tc["arguments"] = val
                elif tc_field == "tool_call.id":
                    tc["id"] = val
    return [messages[i] for i in sorted(messages)]


def from_otel_spans(spans: list[dict[str, Any]], *, run_id: str | None = None) -> Trace:
    """Normalize a list of OpenInference/OTel spans into a canonical :class:`Trace`."""
    ordered = sorted(
        (s for s in spans if isinstance(s, dict)),
        key=lambda s: (_start_key(s), _span_id(s)),
    )
    parsed = [(s, _attrs(s)) for s in ordered]
    has_tool_span = any(str(a.get(_SPAN_KIND) or "").upper() == "TOOL" for _s, a in parsed)

    steps: list[Step] = []
    for span, attrs in parsed:
        kind = str(attrs.get(_SPAN_KIND) or "").upper()

        if kind == "TOOL":
            call_id = _span_id(span) or f"span-{len(steps)}"
            name = str(attrs.get("tool.name") or span.get("name") or "")
            args, raw_text = _args_from(attrs.get("input.value"))
            steps.append(ToolCall(call_id=call_id, name=name, args=args, raw_text=raw_text))
            is_err, err_msg = _is_error_span(span, attrs)
            content, _ = _parse_value(attrs.get("output.value"))
            steps.append(
                ToolResult(
                    call_id=call_id,
                    content=content,
                    status=ResultStatus.ERROR if is_err else ResultStatus.UNKNOWN,
                    error=err_msg,
                )
            )
            continue

        if kind == "LLM":
            for msg in _collect_messages(attrs, "llm.output_messages"):
                if isinstance(msg.get("content"), str) and msg["content"]:
                    steps.append(Message(Role.ASSISTANT, msg["content"]))
                if not has_tool_span:
                    for _i, tc in sorted(msg.get("tool_calls", {}).items()):
                        c_args, c_raw = _args_from(tc.get("arguments"))
                        steps.append(
                            ToolCall(
                                call_id=str(tc.get("id", "")),
                                name=str(tc.get("name", "")),
                                args=c_args,
                                raw_text=c_raw,
                            )
                        )
        # Other span kinds (CHAIN / RETRIEVER / AGENT / EMBEDDING) are skipped — faithful, not
        # clever: we do not invent tool calls from non-tool spans.

    final = None
    for step in reversed(steps):
        if isinstance(step, Message) and step.role is Role.ASSISTANT and step.content:
            final = step.content
            break
    resolved = run_id or str(
        _get(ordered[0], "trace_id", "traceId", "context.trace_id") if ordered else "" or "otel-run"
    )
    return Trace(run_id=resolved or "otel-run", steps=steps, final=final)
