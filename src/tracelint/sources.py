"""Read a trace file in a provider format and lint it — the CLI/library on-ramps.

The rules only ever run against the canonical :class:`~tracelint.trace.Trace` schema, and the
:mod:`tracelint.adapters` already normalize each provider's shape into it. But almost nobody
*emits* the canonical schema — they emit OpenAI messages, Langfuse traces, or
OpenTelemetry/OpenInference spans. This module is the thin layer between "a file on disk in format
X" and ``list[Trace]``, plus the one-call ``lint_*`` wrappers the library exposes
(``from tracelint import lint_otel_trace``). It adds no new detection logic; it only decides which
adapter parses the bytes.

Supported ``--format`` values:

- ``native``      canonical tracelint JSON (``.json`` / ``.jsonl`` / a JSON array) — the default.
- ``openinference`` / ``otel``  OpenTelemetry / OpenInference spans (Arize Phoenix flat-dict, raw
  OTLP ``resourceSpans``, or the Patronus/TRAIL envelope), via :func:`from_otel_spans`.
- ``openai``                 an OpenAI chat-completions message list (or ``{"messages": [...]}``).
- ``langfuse``               a Langfuse trace object (or a JSON array of them).

Consistent with the rest of the tool, a loader never guesses beyond the shapes its adapter
documents. Multi-trace inputs fan out to one :class:`Trace` each: a ``.jsonl`` file (one unit per
line), a JSON array, and an OTLP export carrying several distinct ``trace_id`` s.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tracelint.adapters.langfuse import from_langfuse_trace
from tracelint.adapters.openai import from_openai_messages
from tracelint.adapters.otel import from_otel_spans
from tracelint.findings import LintReport
from tracelint.rules import default_rules, lint_trace
from tracelint.rules.base import Rule
from tracelint.tools import ToolRegistry
from tracelint.trace import Trace, load_traces

# --- Format identifiers -------------------------------------------------------------
NATIVE = "native"
OPENINFERENCE = "openinference"
OTEL = "otel"
OPENAI = "openai"
LANGFUSE = "langfuse"

#: Every value accepted by ``load_source``/``tracelint check --format``. ``openinference`` and
#: ``otel`` are aliases for the same OpenTelemetry/OpenInference reader.
SUPPORTED_FORMATS: tuple[str, ...] = (NATIVE, OPENINFERENCE, OTEL, OPENAI, LANGFUSE)


# --- One-call convenience linters ---------------------------------------------------
# Each normalizes with the matching adapter and runs the default (or caller-supplied) rules.
# ``registry`` carries the tool JSON Schemas that R1 validates against and that upgrade R3 to a
# hard defect; provider traces rarely embed schemas, so it stays optional (schema-dependent rules
# then suppress, never fake a pass).


def lint_otel_trace(
    spans: list[dict[str, Any]],
    rules: list[Rule] | None = None,
    registry: ToolRegistry | None = None,
    *,
    run_id: str | None = None,
) -> LintReport:
    """Lint OpenTelemetry / OpenInference ``spans`` (Phoenix / OTLP / TRAIL) in one call."""
    trace = from_otel_spans(spans, run_id=run_id)
    return lint_trace(trace, rules or default_rules(), registry)


def lint_openai_trace(
    messages: list[dict[str, Any]],
    rules: list[Rule] | None = None,
    registry: ToolRegistry | None = None,
    *,
    run_id: str = "openai-run",
    final: Any = None,
) -> LintReport:
    """Lint an OpenAI chat-completions ``messages`` list in one call."""
    trace = from_openai_messages(messages, run_id=run_id, final=final)
    return lint_trace(trace, rules or default_rules(), registry)


def lint_langfuse_trace(
    trace: Any,
    rules: list[Rule] | None = None,
    registry: ToolRegistry | None = None,
    *,
    tool_names: list[str] | set[str] | None = None,
    run_id: str | None = None,
) -> LintReport:
    """Lint a Langfuse ``trace`` (dict or SDK object) in one call."""
    canonical = from_langfuse_trace(trace, tool_names=tool_names, run_id=run_id)
    return lint_trace(canonical, rules or default_rules(), registry)


# --- File loading + format dispatch -------------------------------------------------


def load_source(
    path: str | Path,
    fmt: str = NATIVE,
    *,
    tool_names: list[str] | set[str] | None = None,
) -> list[Trace]:
    """Load ``path`` in ``fmt`` and return every :class:`Trace` it contains.

    ``native`` uses :func:`~tracelint.trace.load_traces` (unchanged behaviour). For a provider
    format the file is parsed as one JSON document (``.json``) or one unit per line (``.jsonl``),
    then handed to the matching adapter. ``tool_names`` is forwarded to the Langfuse adapter only.
    """
    if fmt in (NATIVE, None):
        return load_traces(path)
    if fmt not in SUPPORTED_FORMATS:
        raise ValueError(
            f"unknown --format {fmt!r}; choose from {', '.join(SUPPORTED_FORMATS)}"
        )

    p = Path(path)
    text = p.read_text(encoding="utf-8")
    if p.suffix == ".jsonl":
        docs = [json.loads(line) for line in text.splitlines() if line.strip()]
    else:
        docs = [json.loads(text)]

    traces: list[Trace] = []
    for doc in docs:
        traces.extend(_traces_from_doc(doc, fmt, tool_names=tool_names))
    return traces


def _traces_from_doc(
    doc: Any, fmt: str, *, tool_names: list[str] | set[str] | None
) -> list[Trace]:
    if fmt in (OPENINFERENCE, OTEL):
        return _otel_traces(doc)
    if fmt == OPENAI:
        return _openai_traces(doc)
    if fmt == LANGFUSE:
        return _langfuse_traces(doc, tool_names=tool_names)
    raise ValueError(f"unknown --format {fmt!r}")  # pragma: no cover - guarded in load_source


# --- OpenTelemetry / OpenInference --------------------------------------------------


def _otel_traces(doc: Any) -> list[Trace]:
    """One trace per distinct ``trace_id`` in ``doc`` (an OTLP export can carry several)."""
    return [from_otel_spans(spans) for spans in _grouped_spans(doc) if spans]


def _extract_spans(doc: Any) -> list[dict[str, Any]]:
    """Pull the span list out of the accepted envelopes (list / OTLP / ``{"spans"}`` / one span)."""
    if isinstance(doc, list):
        return [s for s in doc if isinstance(s, dict)]
    if isinstance(doc, dict):
        for key in ("resourceSpans", "resource_spans"):
            if key in doc:
                return _spans_from_otlp(doc[key])
        for key in ("spans", "data"):
            value = doc.get(key)
            if isinstance(value, list):
                return [s for s in value if isinstance(s, dict)]
        return [doc]  # a single span object
    return []


def _spans_from_otlp(resource_spans: Any) -> list[dict[str, Any]]:
    """Flatten OTLP-JSON ``resourceSpans[].scopeSpans[].spans[]`` into a flat span list."""
    out: list[dict[str, Any]] = []
    for rs in resource_spans or []:
        if not isinstance(rs, dict):
            continue
        scopes = rs.get("scopeSpans") or rs.get("instrumentationLibrarySpans") or []
        for scope in scopes:
            if not isinstance(scope, dict):
                continue
            for span in scope.get("spans") or []:
                if isinstance(span, dict):
                    out.append(span)
    return out


def _span_trace_id(span: dict[str, Any]) -> str:
    for key in ("trace_id", "traceId"):
        value = span.get(key)
        if value:
            return str(value)
    ctx = span.get("context")
    if isinstance(ctx, dict) and ctx.get("trace_id"):
        return str(ctx["trace_id"])
    return ""


def _grouped_spans(doc: Any) -> list[list[dict[str, Any]]]:
    """Group extracted spans by ``trace_id``, only splitting when 2+ distinct ids are present.

    A single-trace export whose spans share (or omit) their ``trace_id`` stays one trace — the
    adapter already merges and orders it. Splitting is reserved for a genuine multi-trace OTLP
    file, so we never fracture one run into fragments over an unset id.
    """
    spans = _extract_spans(doc)
    distinct = {tid for tid in (_span_trace_id(s) for s in spans) if tid}
    if len(distinct) <= 1:
        return [spans] if spans else []

    groups: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = []
    for span in spans:
        tid = _span_trace_id(span) or "__no_trace_id__"
        if tid not in groups:
            groups[tid] = []
            order.append(tid)
        groups[tid].append(span)
    return [groups[tid] for tid in order]


# --- OpenAI chat-completions --------------------------------------------------------


def _openai_traces(doc: Any) -> list[Trace]:
    if isinstance(doc, dict):
        if isinstance(doc.get("messages"), list):
            return [
                from_openai_messages(
                    doc["messages"],
                    run_id=str(doc.get("run_id", "openai-run")),
                    final=doc.get("final"),
                )
            ]
        if "role" in doc:  # a lone message object
            return [from_openai_messages([doc])]
        return []
    if isinstance(doc, list):
        if doc and all(isinstance(m, dict) and "role" in m for m in doc):
            return [from_openai_messages(doc)]  # a single message list
        traces: list[Trace] = []
        for item in doc:
            traces.extend(_openai_traces(item))  # a list of trace objects
        return traces
    return []


# --- Langfuse -----------------------------------------------------------------------


def _langfuse_traces(doc: Any, *, tool_names: list[str] | set[str] | None) -> list[Trace]:
    if isinstance(doc, list):
        traces: list[Trace] = []
        for item in doc:
            traces.extend(_langfuse_traces(item, tool_names=tool_names))
        return traces
    if isinstance(doc, dict):
        return [from_langfuse_trace(doc, tool_names=tool_names)]
    return []
