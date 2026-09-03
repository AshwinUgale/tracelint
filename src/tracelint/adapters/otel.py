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

**Export shapes accepted** (normalized before parsing): a *flat dict* of dotted keys (Arize
Phoenix / most JSON exports); the raw *OTLP* attribute list (``[{"key","value":{...}}]``); and the
**Patronus / TRAIL** envelope — attributes under ``span_attributes``, the name under ``span_name``,
a span *tree* nested via ``child_spans`` (flattened here), ``timestamp`` for ordering, and tool
arguments wrapped as ``{"args": [...], "kwargs": {...}}`` (unwrapped to the real arg dict).

**Faithful, not clever** (the same discipline as the other adapters): an error is recorded only
from a *structured* signal (OTel ERROR status, an exception event, or a status/error field in the
output) — never guessed from free text. Missing fields cause the relevant rules to *suppress*, not
to run on partial data.

*Validated on real TRAIL traces (Patronus, MIT): tracelint reads them and deterministically
localizes tool errors, malformed calls, and excessive-retry loops — the fields above were derived
from actual TRAIL/GAIA spans, not just the spec.*
"""

from __future__ import annotations

import ast
import json
from typing import Any

from tracelint.trace import Message, ResultStatus, Role, Step, ToolCall, ToolResult, Trace

_SPAN_KIND = "openinference.span.kind"

# OTel GenAI semantic convention (OpenLLMetry / Traceloop / the OTel GenAI standard) identifies a
# span by its *operation*, not an openinference.span.kind attribute. Map it to the same vocabulary
# so one event-list reader covers both conventions.
_GENAI_OP = "gen_ai.operation.name"
_GENAI_TOOL_OPS = {"execute_tool", "invoke_tool"}
_GENAI_LLM_OPS = {"chat", "text_completion", "completion", "generate_content"}


def _span_kind(span: dict[str, Any], attrs: dict[str, Any]) -> str:
    """The span kind (``TOOL`` / ``LLM`` / ...), normalized to upper case, across conventions.

    Reads, in order: the ``openinference.span.kind`` *attribute* (instrumentation/OTLP path); a
    top-level ``span_kind`` field (**Arize Phoenix's own trace export**); and the OTel **GenAI**
    convention's ``gen_ai.operation.name`` (``execute_tool`` → TOOL, ``chat`` → LLM). Without the
    first two a Phoenix trace recognizes zero tool spans; without the third, a GenAI/OpenLLMetry
    trace does — in both cases every rule then silently suppresses.
    """
    explicit = str(attrs.get(_SPAN_KIND) or span.get("span_kind") or span.get("spanKind") or "")
    if explicit:
        return explicit.upper()
    op = str(attrs.get(_GENAI_OP) or "").lower()
    if op in _GENAI_TOOL_OPS:
        return "TOOL"
    if op in _GENAI_LLM_OPS:
        return "LLM"
    if op == "invoke_agent":
        return "AGENT"
    return ""


def _input_value(attrs: dict[str, Any]) -> Any:
    """The tool/LLM input: OpenInference ``input.value`` or GenAI's plain ``input``."""
    return attrs.get("input.value") if "input.value" in attrs else attrs.get("input")


def _output_value(attrs: dict[str, Any]) -> Any:
    """The tool/LLM output: OpenInference ``output.value`` or GenAI's plain ``output``."""
    return attrs.get("output.value") if "output.value" in attrs else attrs.get("output")


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


_ATTR_PREFIX = "attributes."


def _attrs(span: dict[str, Any]) -> dict[str, Any]:
    """Return a span's attributes as a flat ``{dotted_key: value}`` map.

    Handles the three shapes a real export uses: a nested ``attributes`` dict of dotted keys
    (standard OTLP/Phoenix span JSON) or the ``span_attributes`` envelope (Patronus/TRAIL); an OTLP
    attribute *list* (``[{"key","value":{...}}]``); and — the shape a Phoenix user actually gets
    from ``px.Client().get_spans_dataframe().to_dict("records")`` — the attributes flattened into
    top-level columns prefixed ``attributes.`` (e.g. ``attributes.tool.name``), which we collect
    with the prefix stripped. Without this last case a dataframe-record span is recognized by kind
    but read with empty args and no output.
    """
    raw = span.get("span_attributes") or span.get("attributes") or span.get("attribute") or {}
    flat: dict[str, Any] = {}
    if isinstance(raw, dict):
        flat = dict(raw)
    elif isinstance(raw, list):  # OTLP: [{"key": "...", "value": {"stringValue": "..."}}, ...]
        for item in raw:
            if isinstance(item, dict) and "key" in item:
                flat[item["key"]] = _otlp_value(item.get("value"))

    # Phoenix dataframe-record shape: attribute columns as top-level "attributes.*" keys.
    for key, value in span.items():
        if not (isinstance(key, str) and key.startswith(_ATTR_PREFIX)):
            continue
        stripped = key[len(_ATTR_PREFIX) :]
        if not stripped or value is None or (isinstance(value, float) and value != value):
            continue  # skip the empty tail and NaN/None (json_normalize fills absent cells)
        flat.setdefault(stripped, value)
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
        _get(
            span,
            "start_time",
            "startTime",
            "startTimeUnixNano",
            "start_time_unix_nano",
            "timestamp",
        )
        or ""
    )


def _flatten_spans(spans: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flatten a span tree — real exports (Patronus / TRAIL) nest children under ``child_spans``."""
    out: list[dict[str, Any]] = []
    for s in spans:
        if isinstance(s, dict):
            out.append(s)
            out.extend(_flatten_spans(s.get("child_spans") or []))
    return out


def _unwrap_call_input(value: dict[str, Any]) -> dict[str, Any]:
    """Unwrap a wrapped tool input (``{"args": [...], "kwargs": {...}}``) into a flat arg dict.

    Real OpenInference exports (smolagents/Patronus in TRAIL) record a tool's arguments as
    positional ``args`` + keyword ``kwargs`` (with occasional framework keys like
    ``sanitize_inputs_outputs``). The real argument dict is ``kwargs`` merged with any positional
    dict; anything else is returned unchanged.
    """
    if "kwargs" not in value and "args" not in value:
        return value
    merged: dict[str, Any] = {}
    args = value.get("args")
    if isinstance(args, list):
        for a in args:
            if isinstance(a, dict):
                merged.update(a)
    kwargs = value.get("kwargs")
    if isinstance(kwargs, dict):
        merged.update(kwargs)
    return merged


def _parse_value(raw: Any) -> tuple[Any, str | None]:
    """Parse an OpenInference ``*.value`` (often a JSON string). Return ``(value, raw_text)``.

    Falls back to ``ast.literal_eval`` when JSON fails: several instrumentations serialize a tool's
    arguments with Python's ``str(dict)`` / ``repr`` (single-quoted keys, ``True``/``None``), which
    is not valid JSON but is a well-formed argument object — not a malformed call. ``literal_eval``
    is safe (literals only, no code execution). Only when *both* fail is the value treated as
    unparsed text (what R6 reads as a malformed argument).
    """
    if raw is None or isinstance(raw, (dict, list, int, float, bool)):
        return raw, None
    if isinstance(raw, str):
        try:
            return json.loads(raw), None
        except (json.JSONDecodeError, ValueError):
            pass
        try:
            parsed = ast.literal_eval(raw)
        except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
            return raw, raw
        if isinstance(parsed, (dict, list)):
            return parsed, None
        return raw, raw
    return raw, None


def _args_from(raw: Any) -> tuple[dict[str, Any], str | None]:
    value, raw_text = _parse_value(raw)
    if isinstance(value, dict):
        unwrapped = _unwrap_call_input(value)
        if unwrapped:
            return unwrapped, None
        return {}, raw if isinstance(raw, str) else None
    return {}, raw_text if isinstance(raw_text, str) else (
        str(value) if value is not None else None
    )


def _status_fields(span: dict[str, Any]) -> tuple[str, str | None]:
    """The span's status code (upper-cased) and message across the shapes exports actually use.

    A flat ``status_code`` string (Phoenix span export); a nested ``status`` object as the OTel SDK
    serializes it via ``ReadableSpan.to_json`` (``{"status_code": "ERROR", "description": ...}``);
    and OTLP-JSON (``{"code": "STATUS_CODE_ERROR"}`` or the numeric ``2``). Reading only the flat
    string missed a real SDK-exported ERROR whose failure wasn't also echoed in the output payload.
    """
    code: Any = _get(span, "status_code", "statusCode")
    message: Any = _get(span, "status_message", "statusMessage")
    status = span.get("status")
    if isinstance(status, dict):
        code = code if code is not None else status.get("status_code", status.get("code"))
        if message is None:
            message = status.get("description", status.get("message"))
    elif isinstance(status, str) and code is None:
        code = status
    return str(code if code is not None else "").upper(), (str(message) if message else None)


def _is_error_span(span: dict[str, Any], attrs: dict[str, Any]) -> tuple[bool, str | None]:
    status, message = _status_fields(span)
    # "ERROR" covers the plain code and OTLP's "STATUS_CODE_ERROR"; "2" is OTLP's numeric ERROR.
    if "ERROR" in status or status == "2":
        return True, message
    # ``events`` may be a numpy array (Phoenix get_spans_dataframe records), so avoid truthiness
    # tests on it — ``array or []`` raises "truth value of an array is ambiguous".
    events = span.get("events")
    for event in events if events is not None else []:
        if isinstance(event, dict) and str(event.get("name", "")).lower() == "exception":
            ev_attrs = event.get("attributes", {})
            if isinstance(ev_attrs, list):
                ev_attrs = {i.get("key"): _otlp_value(i.get("value")) for i in ev_attrs}
            return True, str(ev_attrs.get("exception.message") or "exception")
    out = _output_value(attrs)
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
        msg = messages.setdefault(
            idx, {"role": None, "content": None, "content_parts": {}, "tool_calls": {}}
        )
        if tail == "message.role":
            msg["role"] = val
        elif tail == "message.content":
            msg["content"] = val
        elif tail.startswith("message.contents."):
            # OpenInference *content-parts* shape (emitted by smolagents and other instrumentors):
            # ``message.contents.<i>.message_content.text`` — content is a list of typed parts, not
            # a flat string. Reading only ``message.content`` dropped the user's own request, which
            # manufactured R3 hallucination false-positives (the arg value was in the prompt all
            # along). Collect the text parts here; they are joined into ``content`` below.
            part_rest = tail[len("message.contents.") :]
            pi, _, pfield = part_rest.partition(".")
            if pi.isdigit() and pfield == "message_content.text" and isinstance(val, str):
                msg["content_parts"][int(pi)] = val
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
    result: list[dict[str, Any]] = []
    for i in sorted(messages):
        msg = messages[i]
        # A flat ``message.content`` wins; fall back to the joined content-parts text.
        if msg["content"] is None and msg["content_parts"]:
            msg["content"] = "".join(msg["content_parts"][k] for k in sorted(msg["content_parts"]))
        result.append(msg)
    return result


def _seed_input_messages(parsed: list[tuple[dict[str, Any], dict[str, Any]]]) -> list[Message]:
    """Leading user/system turns from the first LLM span's ``llm.input_messages``.

    OpenInference records what the model was *asked* under ``llm.input_messages.*`` — the user's
    request and any system prompt. Without these the trace has no record of what the agent
    observed, so provenance (R3) reports every string argument as underivable: the user's own
    question is in the trace, and dropping it manufactures false hallucination candidates.

    Only the **first** LLM span is read: each later LLM call replays the entire prior conversation
    in its input messages, so seeding from all of them would duplicate every turn. Assistant/tool
    replay turns are skipped (they are emitted from their own spans); only the opening user/system
    context is seeded, once, at the front so it precedes every tool call for provenance ordering.
    """
    for span, attrs in parsed:
        if _span_kind(span, attrs) != "LLM":
            continue
        input_messages = _collect_messages(attrs, "llm.input_messages")
        if input_messages:
            seeded: list[Message] = []
            for msg in input_messages:
                role = str(msg.get("role") or "").lower()
                content = msg.get("content")
                if role in ("user", "system") and isinstance(content, str) and content:
                    seeded.append(Message(Role.USER if role == "user" else Role.SYSTEM, content))
            return seeded
        genai_seeded = _genai_input_seed(attrs)
        if genai_seeded:
            return genai_seeded
    return []


def _genai_text(message: dict[str, Any]) -> str | None:
    """Text of a GenAI message — a flat ``content`` string, or the ``text`` parts joined."""
    if isinstance(message.get("content"), str):
        return message["content"] or None
    parts = message.get("parts")
    if isinstance(parts, list):
        texts = [
            p["content"]
            for p in parts
            if isinstance(p, dict) and p.get("type") == "text" and isinstance(p.get("content"), str)
        ]
        return "\n".join(t for t in texts if t) or None
    return None


def _genai_input_seed(attrs: dict[str, Any]) -> list[Message]:
    """Leading user/system turns from GenAI ``gen_ai.input.messages`` (JSON string, parts)."""
    parsed, _ = _parse_value(attrs.get("gen_ai.input.messages"))
    if not isinstance(parsed, list):
        return []
    seeded: list[Message] = []
    for message in parsed:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "").lower()
        if role not in ("user", "system"):
            continue
        text = _genai_text(message)
        if text:
            seeded.append(Message(Role.USER if role == "user" else Role.SYSTEM, text))
    return seeded


def from_otel_spans(spans: list[dict[str, Any]], *, run_id: str | None = None) -> Trace:
    """Normalize a list of OpenInference/OTel spans into a canonical :class:`Trace`."""
    ordered = sorted(
        _flatten_spans(spans),
        key=lambda s: (_start_key(s), _span_id(s)),
    )
    parsed = [(s, _attrs(s)) for s in ordered]
    has_tool_span = any(_span_kind(s, a) == "TOOL" for s, a in parsed)

    steps: list[Step] = []
    steps.extend(_seed_input_messages(parsed))
    for span, attrs in parsed:
        kind = _span_kind(span, attrs)

        if kind == "TOOL":
            call_id = (
                _span_id(span)
                or str(attrs.get("gen_ai.tool.call.id") or "")
                or f"span-{len(steps)}"
            )
            name = str(
                attrs.get("tool.name")
                or attrs.get("gen_ai.tool.name")
                or span.get("span_name")
                or span.get("name")
                or ""
            )
            args, raw_text = _args_from(_input_value(attrs))
            steps.append(ToolCall(call_id=call_id, name=name, args=args, raw_text=raw_text))
            is_err, err_msg = _is_error_span(span, attrs)
            content, _ = _parse_value(_output_value(attrs))
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
