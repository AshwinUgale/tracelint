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
- ``langsmith``              a LangSmith run tree (or a JSON array of them).
- ``atif``                   a Harbor ATIF trajectory (``agent/trajectory.json``), or a JSON array
  of them; each embedded subagent trajectory is linted as its own run.

Consistent with the rest of the tool, a loader never guesses beyond the shapes its adapter
documents. Multi-trace inputs fan out to one :class:`Trace` each: a JSON array, an OTLP export
carrying several distinct ``trace_id`` s, and a ``.jsonl`` file. For the span formats a run is
defined by its trace id, not by a line or a file: an exporter writes a span (or a batch) per line,
so a ``.jsonl`` file is read as one span collection and regrouped by trace id, and ``check`` merges
a run whose spans are split across files. For OpenAI, a ``.jsonl`` file of single messages is one
conversation; one ``{"messages": [...]}`` (or ShareGPT ``{"conversations": [...]}``) per line is
one conversation per line.

A file that yields nothing to lint is an input error, never a clean pass: an empty file, or one in
which nothing reads as a trace in the requested format — the wrong ``--format``, which the error
names (read as native, the default, a span file used to become one empty "trace" per span).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tracelint.adapters.atif import from_atif_trajectories, from_atif_trajectory, is_atif
from tracelint.adapters.langfuse import from_langfuse_trace
from tracelint.adapters.langsmith import from_langsmith_run
from tracelint.adapters.openai import from_openai_messages
from tracelint.adapters.otel import from_otel_spans, span_trace_id
from tracelint.findings import LintReport
from tracelint.rules import default_rules, lint_trace
from tracelint.rules.base import Rule
from tracelint.tools import ToolRegistry
from tracelint.trace import Trace

# --- Format identifiers -------------------------------------------------------------
NATIVE = "native"
OPENINFERENCE = "openinference"
OTEL = "otel"
OPENAI = "openai"
LANGFUSE = "langfuse"
LANGSMITH = "langsmith"
ATIF = "atif"

#: Every value accepted by ``load_source``/``tracelint check --format``. ``openinference`` and
#: ``otel`` are aliases for the same OpenTelemetry/OpenInference reader.
SUPPORTED_FORMATS: tuple[str, ...] = (
    NATIVE,
    OPENINFERENCE,
    OTEL,
    OPENAI,
    LANGFUSE,
    LANGSMITH,
    ATIF,
)


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
    """Lint one trace's OpenTelemetry / OpenInference ``spans`` (Phoenix / OTLP / TRAIL).

    ``spans`` must come from a single trace; spans spanning several traces raise ``ValueError``
    (use :func:`lint_otel_traces` for those).
    """
    trace = from_otel_spans(spans, run_id=run_id)
    return lint_trace(trace, rules or default_rules(), registry)


def lint_otel_traces(
    spans: list[dict[str, Any]],
    rules: list[Rule] | None = None,
    registry: ToolRegistry | None = None,
) -> list[LintReport]:
    """Lint OpenInference spans from any number of traces — one :class:`LintReport` per trace.

    The shape a Phoenix project export actually has: ``Client().spans.get_spans_dataframe(...)
    .to_dict("records")`` returns every run in the project, grouped here by trace id.
    """
    active = rules or default_rules()
    return [lint_trace(trace, active, registry) for trace in _otel_traces(spans)]


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


def lint_langsmith_trace(
    run: Any,
    rules: list[Rule] | None = None,
    registry: ToolRegistry | None = None,
    *,
    run_id: str | None = None,
) -> LintReport:
    """Lint a LangSmith run tree in one call."""
    trace = from_langsmith_run(run, run_id=run_id)
    return lint_trace(trace, rules or default_rules(), registry)


def lint_atif_trajectory(
    trajectory: dict[str, Any],
    rules: list[Rule] | None = None,
    registry: ToolRegistry | None = None,
    *,
    run_id: str | None = None,
) -> LintReport:
    """Lint a Harbor ATIF trajectory (its own steps; lint embedded subagents with
    :func:`~tracelint.adapters.from_atif_trajectories`) in one call."""
    trace = from_atif_trajectory(trajectory, run_id=run_id)
    return lint_trace(trace, rules or default_rules(), registry)


# --- File loading + format dispatch -------------------------------------------------


def load_source(
    path: str | Path,
    fmt: str = NATIVE,
    *,
    tool_names: list[str] | set[str] | None = None,
) -> list[Trace]:
    """Load ``path`` in ``fmt`` and return every :class:`Trace` it contains.

    The file is parsed as one JSON document (``.json``) or one document per line (``.jsonl``),
    then handed to the matching adapter; span formats regroup the file's spans by trace id (see
    the module docstring). ``tool_names`` is forwarded to the Langfuse adapter only.

    Raises :class:`ValueError` when the file holds nothing to lint — it is empty, a native
    document is not a native trace, or nothing in it reads as ``fmt`` (the message names the format
    it looks like).
    """
    if fmt is None:
        fmt = NATIVE
    if fmt not in SUPPORTED_FORMATS:
        raise ValueError(f"unknown --format {fmt!r}; choose from {', '.join(SUPPORTED_FORMATS)}")
    if fmt == NATIVE:
        traces = _native_traces(path)
    elif fmt in (OPENINFERENCE, OTEL):
        traces = [from_otel_spans(spans) for spans in _span_groups(path) if spans]
    else:
        traces = [
            trace
            for doc in _read_docs(path, fmt)
            for trace in _traces_from_doc(doc, fmt, tool_names=tool_names)
        ]
    if not any(trace.steps for trace in traces):
        raise _nothing_to_lint(path, fmt)
    return traces


def load_sources(
    paths: list[str | Path],
    fmt: str = NATIVE,
    *,
    tool_names: list[str] | set[str] | None = None,
) -> list[tuple[Trace, str]]:
    """Load every file in ``paths`` and return ``(trace, path)`` for each trace — what ``check``
    lints.

    For the span formats a run is its trace id, wherever its spans landed: a rotating exporter or a
    glob over batch files can split one run across files, and linting the pieces separately would
    hide its cross-step defects and invent partial-run ones. Those pieces are merged (a span seen
    twice, e.g. the same file passed twice, is kept once) and reported under the first file. Other
    formats are one or more self-contained traces per file, read with :func:`load_source`.
    """
    if fmt not in (OPENINFERENCE, OTEL):
        return [
            (trace, str(path))
            for path in paths
            for trace in load_source(path, fmt, tool_names=tool_names)
        ]

    runs: dict[str, list[dict[str, Any]]] = {}
    first_seen: dict[str, str] = {}
    contributed: dict[str, list[str]] = {}  # path -> the runs its spans belong to
    for path in paths:
        keys = contributed.setdefault(str(path), [])
        for i, spans in enumerate(_span_groups(path)):
            trace_id = next((tid for tid in map(_span_trace_id, spans) if tid), "")
            # A run without a trace id can't be matched to another file's, so it never merges.
            key = trace_id or f"\x00{path}#{i}"
            keys.append(key)
            if key not in runs:
                runs[key], first_seen[key] = list(spans), str(path)
                continue
            seen = {_span_key(span) for span in runs[key]}
            runs[key].extend(span for span in spans if _span_key(span) not in seen)
    traces = {key: from_otel_spans(spans) for key, spans in runs.items()}
    # Checked per file, after merging: a file holding only part of a run is fine when the run as a
    # whole has something to lint.
    for path, keys in contributed.items():
        if not any(traces[key].steps for key in keys):
            raise _nothing_to_lint(path, fmt)
    return [(traces[key], first_seen[key]) for key in runs]


def _read_docs(path: str | Path, fmt: str) -> list[Any]:
    """The JSON documents in ``path``: the whole file (``.json``), or one per line (``.jsonl``)."""
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    if p.suffix != ".jsonl":
        return [json.loads(text)]
    docs = [json.loads(line) for line in text.splitlines() if line.strip()]
    if fmt == OPENAI and docs and all(_is_lone_message(doc) for doc in docs):
        return [docs]  # one message per line: the file is a single conversation
    return docs


def _native_traces(path: str | Path) -> list[Trace]:
    """Native traces from ``path`` — each a JSON object with a ``steps`` list (one per line in a
    ``.jsonl``; a ``.json`` file may hold one or a JSON array of them). Anything else is not a
    native trace and is rejected, naming the format it looks like, rather than read as an empty
    trace."""
    jsonl = Path(path).suffix == ".jsonl"
    docs = _read_docs(path, NATIVE)
    if jsonl:
        units = docs
    else:
        units = [unit for doc in docs for unit in (doc if isinstance(doc, list) else [doc])]
    for i, unit in enumerate(units):
        # An ATIF trajectory has a ``steps`` list too, but its steps are not native steps.
        if not (isinstance(unit, dict) and isinstance(unit.get("steps"), list)) or is_atif(unit):
            where = f"{path}, line {i + 1}" if jsonl else str(path)
            raise _wrong_format(where, NATIVE, unit)
    return [Trace.from_dict(unit) for unit in units]


# What each --format reads, for error messages.
_FORMAT_NAMES = {
    NATIVE: "native tracelint traces",
    OPENINFERENCE: "OpenInference / OTel spans",
    OTEL: "OpenInference / OTel spans",
    OPENAI: "OpenAI chat messages",
    LANGFUSE: "Langfuse traces",
    LANGSMITH: "LangSmith runs",
    ATIF: "Harbor ATIF trajectories",
}

# Keys only a span record carries (flat Phoenix columns, OTel SDK / OTLP spans, TRAIL). A trace id
# alone is no evidence: Langfuse observations and LangSmith runs carry one too.
_SPAN_KEYS = frozenset(
    {
        "span_id",
        "spanId",
        "context.span_id",
        "context",
        "span_kind",
        "attributes",
        "span_attributes",
    }
)


def _looks_like(doc: Any) -> str | None:
    """Which ``--format`` ``doc`` appears to be in — only to name it in an error, never to choose
    one (shapes overlap, so a guess must not decide what the rules read)."""
    if isinstance(doc, list):
        return next((fmt for fmt in map(_looks_like, doc) if fmt), None)
    if not isinstance(doc, dict):
        return None
    if is_atif(doc):  # before native: an ATIF trajectory also has a ``steps`` list
        return ATIF
    if isinstance(doc.get("steps"), list):
        return NATIVE
    if "resourceSpans" in doc or "resource_spans" in doc:
        return OPENINFERENCE
    if "observations" in doc:
        return LANGFUSE
    if any(key in doc for key in ("run_type", "runType", "child_runs", "childRuns")):
        return LANGSMITH
    if _conversation(doc) is not None or _is_lone_message(doc):
        return OPENAI
    if any(key in _SPAN_KEYS or str(key).startswith("attributes.") for key in doc):
        return OPENINFERENCE
    return None


def _wrong_format(where: str, fmt: str, doc: Any) -> ValueError:
    """The error for input that is not ``fmt``: what it looks like, and the flag to read it."""
    looks = _looks_like(doc)
    if looks is None or _FORMAT_NAMES[looks] == _FORMAT_NAMES[fmt]:
        hint = "check --format (" + ", ".join(SUPPORTED_FORMATS) + ")"
    elif looks == NATIVE:
        hint = "it looks like a native tracelint trace: omit --format (native is the default)"
    else:
        hint = f"it looks like {_FORMAT_NAMES[looks]}: use --format {looks}"
    return ValueError(f"{where}: not readable as {_FORMAT_NAMES[fmt]} (--format {fmt}); {hint}")


def _nothing_to_lint(path: str | Path, fmt: str) -> ValueError:
    """The error for a file that yields no tool calls or messages when read as ``fmt``."""
    docs = _read_docs(path, fmt)
    if not any(docs):  # no lines, ``[]``, ``{}``
        return ValueError(f"{path}: no traces found — the file is empty")
    looks = _looks_like(docs)
    if looks is not None and _FORMAT_NAMES[looks] != _FORMAT_NAMES[fmt]:
        return _wrong_format(str(path), fmt, docs)
    return ValueError(
        f"{path}: nothing to lint — no tool calls or messages found reading it as "
        f"{_FORMAT_NAMES[fmt]} (--format {fmt})"
    )


def _traces_from_doc(doc: Any, fmt: str, *, tool_names: list[str] | set[str] | None) -> list[Trace]:
    if fmt == OPENAI:
        return _openai_traces(doc)
    if fmt == LANGFUSE:
        return _langfuse_traces(doc, tool_names=tool_names)
    if fmt == LANGSMITH:
        return _langsmith_traces(doc)
    if fmt == ATIF:
        return _atif_traces(doc)
    raise ValueError(f"unknown --format {fmt!r}")  # pragma: no cover - guarded in load_source


# --- OpenTelemetry / OpenInference --------------------------------------------------


def _otel_traces(doc: Any) -> list[Trace]:
    """One trace per distinct ``trace_id`` in ``doc`` (an OTLP export can carry several)."""
    return [from_otel_spans(spans) for spans in _group_by_trace(_extract_spans(doc)) if spans]


def _span_groups(path: str | Path) -> list[list[dict[str, Any]]]:
    """A span file's spans, grouped by trace id. A ``.jsonl`` file is one span collection — its
    lines may each hold a span, a whole run, or an OTLP batch — so it is grouped exactly as if the
    lines were concatenated into one JSON array, never line by line. The one exception: with no
    trace ids to regroup by, lines that each hold a whole run stay separate runs (merging them would
    invent loops and repeats across unrelated runs)."""
    docs = _read_docs(path, OPENINFERENCE)
    per_doc = [_extract_spans(doc) for doc in docs]
    spans = [span for group in per_doc for span in group]
    if (
        len(docs) > 1
        and all(_is_span_collection(doc) for doc in docs)
        and not any(_span_trace_id(span) for span in spans)
    ):
        return [group for group in per_doc if group]
    return _group_by_trace(spans)


def _is_span_collection(doc: Any) -> bool:
    """A document holding many spans (a list, an OTLP export, ``{"spans": [...]}``), not one."""
    if isinstance(doc, list):
        return True
    if not isinstance(doc, dict):
        return False
    if "resourceSpans" in doc or "resource_spans" in doc:
        return True
    return any(isinstance(doc.get(key), list) for key in ("spans", "data"))


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
    # Shared with the adapter so grouping and the adapter's one-trace guard agree on what a trace
    # id is — including the Phoenix dataframe's flat ``context.trace_id`` column, which a bare
    # ``trace_id`` lookup missed (a multi-run Phoenix export was linted as one merged trace).
    return span_trace_id(span)


def _span_key(span: dict[str, Any]) -> str:
    """Identity of a span for de-duplicating merged runs: its full content."""
    return json.dumps(span, sort_keys=True, default=str)


def _group_by_trace(spans: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Group spans by ``trace_id``, only splitting when 2+ distinct ids are present.

    A single-trace export whose spans share (or omit) their ``trace_id`` stays one trace — the
    adapter already merges and orders it. Splitting is reserved for a genuine multi-trace export,
    so we never fracture one run into fragments over an unset id.
    """
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


def _conversation(doc: dict[str, Any]) -> list[Any] | None:
    """The message list of a conversation object: OpenAI's ``{"messages": [...]}`` (the
    fine-tuning / batch format) or ShareGPT's ``{"conversations": [...]}``."""
    for key in ("messages", "conversations"):
        if isinstance(doc.get(key), list):
            return doc[key]
    return None


def _is_lone_message(doc: Any) -> bool:
    """A single chat message object — ``role`` (OpenAI) or ``from`` (ShareGPT) — not a
    conversation."""
    return isinstance(doc, dict) and ("role" in doc or "from" in doc) and _conversation(doc) is None


def _openai_traces(doc: Any) -> list[Trace]:
    if isinstance(doc, dict):
        messages = _conversation(doc)
        if messages is not None:
            return [
                from_openai_messages(
                    messages,
                    run_id=str(doc.get("run_id", "openai-run")),
                    final=doc.get("final"),
                )
            ]
        if _is_lone_message(doc):
            return [from_openai_messages([doc])]
        return []
    if isinstance(doc, list):
        if doc and all(_is_lone_message(m) for m in doc):
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


def _langsmith_traces(doc: Any) -> list[Trace]:
    if isinstance(doc, list):
        traces: list[Trace] = []
        for item in doc:
            traces.extend(_langsmith_traces(item))
        return traces
    if isinstance(doc, dict):
        return [from_langsmith_run(doc)]
    return []


# --- Harbor ATIF --------------------------------------------------------------------


def _atif_traces(doc: Any) -> list[Trace]:
    """Each ATIF trajectory in ``doc`` (one, or a JSON array), plus its embedded subagents. A
    document that is not ATIF yields nothing, so the error names the format it looks like."""
    if isinstance(doc, list):
        traces: list[Trace] = []
        for item in doc:
            traces.extend(_atif_traces(item))
        return traces
    if is_atif(doc):
        return from_atif_trajectories(doc)
    return []
