"""Capture an agent run to a lintable trace file — the missing on-ramp.

`tracelint check` reads a trace file, but most people don't have one yet. This module closes that
gap with a one-call context manager that records a run to disk in a format `check` already accepts::

    from tracelint.capture import capture

    with capture("trace.json", framework="smolagents"):
        agent.run("...")
    # → tracelint check trace.json --format openinference

**It wraps existing instrumentation — it does not reinvent it.** Each supported framework already
ships an OpenInference instrumentor (the same one the adoption tests validated); `capture` stands up
a *local* OpenTelemetry provider whose only exporter writes spans to the file, activates that
framework's instrumentor against it, and tears it down on exit. The output is the flat OpenInference
span shape the OTel adapter reads, so a captured trace lints identically to a hand-exported one. The
only per-framework knowledge here is a name → instrumentor lookup (:data:`_INSTRUMENTORS`); the
capture mechanism itself is uniform.

The provider is local and never installed globally, so a user's own tracing (Phoenix, Langfuse) is
untouched while capture runs. The OpenTelemetry SDK is an optional dependency
(``pip install "tracelint[capture]"``) plus the per-framework instrumentor
(``tracelint[capture-smolagents]`` / ``[capture-langchain]`` / ``[capture-crewai]``); both are
imported lazily, so importing tracelint never requires them.

Without a ``framework`` the manager just yields a tracer and captures whatever spans are emitted on
it — the manual escape hatch, and how the offline test exercises the exporter with no framework or
network.
"""

from __future__ import annotations

import importlib
import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

# framework name -> (instrumentor module, class). LangGraph is built on LangChain and shares its
# OpenInference instrumentor (the same one Langflow's Phoenix tracer uses).
_INSTRUMENTORS: dict[str, tuple[str, str]] = {
    "smolagents": ("openinference.instrumentation.smolagents", "SmolagentsInstrumentor"),
    "langchain": ("openinference.instrumentation.langchain", "LangChainInstrumentor"),
    "langgraph": ("openinference.instrumentation.langchain", "LangChainInstrumentor"),
    "crewai": ("openinference.instrumentation.crewai", "CrewAIInstrumentor"),
}

# framework name -> the `tracelint[capture-<suffix>]` extra that installs its instrumentor.
_EXTRA: dict[str, str] = {
    "smolagents": "smolagents",
    "langchain": "langchain",
    "langgraph": "langchain",
    "crewai": "crewai",
}

#: Frameworks the in-process capture helper supports.
SUPPORTED_FRAMEWORKS: tuple[str, ...] = tuple(_INSTRUMENTORS)


def _span_to_dict(span: Any) -> dict[str, Any]:
    """Serialize a finished OTel ``ReadableSpan`` into the flat OpenInference span dict.

    This is the shape the OTel adapter reads natively (top-level ``span_id`` / ``trace_id`` /
    ``status_code``, attributes as a flat dotted map) and the exact shape the validated
    ``examples/traces/*.json`` fixtures use, so a captured trace lints identically to a real export.
    """
    ctx = span.get_span_context()
    parent = getattr(span, "parent", None)
    status = getattr(span, "status", None)
    return {
        "name": span.name,
        "span_id": format(ctx.span_id, "016x"),
        "trace_id": format(ctx.trace_id, "032x"),
        "parent_id": format(parent.span_id, "016x") if parent is not None else None,
        "start_time": span.start_time,
        "end_time": span.end_time,
        "status_code": status.status_code.name if status is not None else "UNSET",
        "status_message": status.description if status is not None else None,
        "attributes": dict(span.attributes or {}),
        "events": [
            {"name": e.name, "timestamp": e.timestamp, "attributes": dict(e.attributes or {})}
            for e in (getattr(span, "events", None) or [])
        ],
    }


def _make_file_exporter() -> Any:
    """A ``SpanExporter`` buffering finished spans as flat OpenInference dicts (lazy import)."""
    from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

    class _FileSpanExporter(SpanExporter):
        def __init__(self) -> None:
            self.spans: list[dict[str, Any]] = []

        def export(self, spans: Any) -> Any:
            self.spans.extend(_span_to_dict(s) for s in spans)
            return SpanExportResult.SUCCESS

        def shutdown(self) -> None:  # nothing to release; the buffer is written on exit
            pass

        def force_flush(self, timeout_millis: int = 30_000) -> bool:
            return True

    return _FileSpanExporter()


def _load_instrumentor(framework: str) -> Any:
    """Import and instantiate the framework's OpenInference instrumentor, or say how to get it."""
    module_path, class_name = _INSTRUMENTORS[framework]
    try:
        module = importlib.import_module(module_path)
    except ImportError as exc:  # pragma: no cover - exercised only without the extra installed
        raise RuntimeError(
            f"capturing {framework!r} needs its OpenInference instrumentor — install it with "
            f'`pip install "tracelint[capture-{_EXTRA[framework]}]"`'
        ) from exc
    return getattr(module, class_name)()


def _new_provider() -> Any:
    """A fresh, local ``TracerProvider`` — never the global one, so real tracing is untouched."""
    try:
        from opentelemetry.sdk.trace import TracerProvider
    except ImportError as exc:  # pragma: no cover - exercised only without the extra installed
        raise RuntimeError(
            "capture needs the OpenTelemetry SDK — install it with "
            '`pip install "tracelint[capture]"` (or a per-framework extra like '
            '"tracelint[capture-smolagents]")'
        ) from exc
    return TracerProvider()


@contextmanager
def capture(path: str | Path, framework: str | None = None) -> Iterator[Any]:
    """Record the agent run inside the ``with`` block to ``path`` as an OpenInference trace.

    ``framework`` selects the OpenInference instrumentor to wrap (see :data:`SUPPORTED_FRAMEWORKS`);
    lint the result with ``tracelint check <path> --format openinference``. With ``framework=None``
    no instrumentor is activated and the yielded tracer captures only spans emitted on it directly.

    The written file is a JSON array of flat OpenInference spans. The provider is local and torn
    down on exit; any tracing the caller already had configured is left in place.
    """
    if framework is not None and framework not in _INSTRUMENTORS:
        raise ValueError(
            f"unknown framework {framework!r}; choose from {', '.join(SUPPORTED_FRAMEWORKS)} "
            "(or omit it and emit spans on the yielded tracer)"
        )

    from opentelemetry.sdk.trace.export import SimpleSpanProcessor

    provider = _new_provider()
    exporter = _make_file_exporter()
    # SimpleSpanProcessor exports each span as it finishes — deterministic for short test runs,
    # unlike the batch processor which may drop spans if the process exits before its flush.
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    instrumentor = _load_instrumentor(framework) if framework is not None else None
    if instrumentor is not None:
        instrumentor.instrument(tracer_provider=provider)
    try:
        yield provider.get_tracer("tracelint.capture")
    finally:
        if instrumentor is not None:
            instrumentor.uninstrument()
        provider.force_flush()
        Path(path).write_text(json.dumps(exporter.spans, indent=2) + "\n", encoding="utf-8")
        provider.shutdown()
