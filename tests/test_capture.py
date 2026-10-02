"""`tracelint.capture` produces a trace file that `tracelint check --format openinference` accepts.

The capture *mechanism* — a local OTel provider whose file exporter writes the flat OpenInference
span shape — is what these tests pin, exercised with no framework, no API key, and no network
(``framework=None`` + spans emitted on the yielded tracer). The per-framework instrumentor wiring is
a thin lookup validated by the real-trace fixtures in ``test_framework_examples.py``; it needs a
live framework + model, so it is not run in CI.
"""

from __future__ import annotations

import json

import pytest

from tracelint.sources import load_source

pytest.importorskip("opentelemetry.sdk")


def _emit_tool_span(tracer, name, args, output):
    """Emit one OpenInference TOOL span (call + result) on ``tracer`` — a minimal captured run."""
    with tracer.start_as_current_span(name) as span:
        span.set_attribute("openinference.span.kind", "TOOL")
        span.set_attribute("tool.name", name)
        span.set_attribute("input.value", json.dumps(args))
        span.set_attribute("output.value", json.dumps(output))


def test_capture_writes_a_lintable_openinference_trace(tmp_path):
    from tracelint.capture import capture

    out = tmp_path / "trace.json"
    with capture(out) as tracer:
        _emit_tool_span(tracer, "get_order", {"order_id": "A100"}, {"status": "confirmed"})

    assert out.exists()
    # The file is a JSON array of flat OpenInference spans (same shape as examples/traces/*.json).
    spans = json.loads(out.read_text(encoding="utf-8"))
    assert isinstance(spans, list) and spans
    assert spans[0]["attributes"]["openinference.span.kind"] == "TOOL"

    # And it round-trips through the real loader: the captured tool call is recovered.
    traces = load_source(out, "openinference")
    assert len(traces) == 1
    calls = traces[0].tool_calls()
    assert [c.name for c in calls] == ["get_order"]
    assert calls[0].args == {"order_id": "A100"}


def test_capture_rejects_an_unknown_framework(tmp_path):
    from tracelint.capture import capture

    with pytest.raises(ValueError, match="unknown framework"):
        with capture(tmp_path / "trace.json", framework="not-a-framework"):
            pass  # pragma: no cover - the manager raises before the body runs

    # A rejected framework must not leave a partial/empty trace file behind.
    assert not (tmp_path / "trace.json").exists()


def test_an_empty_capture_raises_instead_of_writing_a_clean_looking_trace(tmp_path):
    # An empty capture used to write `[]`, which linted as a clean run.
    from tracelint.capture import capture

    with pytest.raises(RuntimeError, match="recorded no spans"):
        with capture(tmp_path / "trace.json"):
            pass


def test_an_error_in_the_block_is_not_masked_by_the_empty_capture(tmp_path):
    from tracelint.capture import capture

    with pytest.raises(KeyError, match="agent crashed"):
        with capture(tmp_path / "trace.json"):
            raise KeyError("agent crashed")


def test_capture_without_the_sdk_says_how_to_install_it(tmp_path, monkeypatch):
    # The span-processor import used to run before the friendly message, so a missing extra
    # surfaced as a raw ModuleNotFoundError.
    import sys

    from tracelint.capture import capture

    monkeypatch.setitem(sys.modules, "opentelemetry.sdk.trace", None)
    monkeypatch.setitem(sys.modules, "opentelemetry.sdk.trace.export", None)
    with pytest.raises(RuntimeError, match=r'pip install "tracelint\[capture\]"'):
        with capture(tmp_path / "trace.json"):
            pass  # pragma: no cover - the manager raises before the body runs


# --- A framework the caller has already instrumented -------------------------------------------
# OpenInference instrumentors are process-wide singletons. These tests stand in a minimal one (the
# same interface capture uses) so they run in CI without any framework installed; the real
# smolagents and LangChain instrumentors behave the same way.


class _FakeInstrumentor:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance.provider = None
        return cls._instance

    @property
    def is_instrumented_by_opentelemetry(self):
        return self.provider is not None

    def instrument(self, tracer_provider=None):
        if self.provider is None:  # a second instrument() is a no-op, as in the real ones
            self.provider = tracer_provider

    def uninstrument(self):
        self.provider = None


def _run_fake_agent():
    """The 'framework': emits one TOOL span through whatever provider instrumented it."""
    provider = _FakeInstrumentor().provider
    if provider is not None:
        _emit_tool_span(provider.get_tracer("fake"), "get_order", {"order_id": "A100"}, {})


@pytest.fixture
def fakefw(monkeypatch):
    import sys
    import types

    from tracelint import capture as capture_module

    module = types.ModuleType("tracelint_test_fakefw")
    module.FakeInstrumentor = _FakeInstrumentor
    monkeypatch.setitem(sys.modules, "tracelint_test_fakefw", module)
    monkeypatch.setitem(
        capture_module._INSTRUMENTORS, "fakefw", ("tracelint_test_fakefw", "FakeInstrumentor")
    )
    monkeypatch.setitem(capture_module._EXTRA, "fakefw", "fakefw")
    _FakeInstrumentor._instance = None
    yield
    _FakeInstrumentor._instance = None


def _user_tracing():
    """The caller's own tracing: an SDK provider exporting to an in-memory 'backend'."""
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    backend = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(backend))
    return provider, backend


def _captured(path):
    return [s["name"] for s in json.loads(path.read_text(encoding="utf-8"))]


def test_capture_instruments_a_fresh_framework_and_restores_it(tmp_path, fakefw):
    from tracelint.capture import capture

    out = tmp_path / "trace.json"
    with capture(out, framework="fakefw"):
        _run_fake_agent()
    assert _captured(out) == ["get_order"]
    assert not _FakeInstrumentor().is_instrumented_by_opentelemetry


def test_existing_instrumentation_on_the_global_provider_is_captured_and_kept(
    tmp_path, fakefw, monkeypatch
):
    # The phoenix.otel.register() setup: the caller's provider is the global one. 0.8-0.9
    # recorded nothing here and then uninstrumented the caller's tracing for the rest of the run.
    from opentelemetry import trace as trace_api

    from tracelint.capture import capture

    provider, backend = _user_tracing()
    monkeypatch.setattr(trace_api, "get_tracer_provider", lambda: provider)
    _FakeInstrumentor().instrument(tracer_provider=provider)

    out = tmp_path / "trace.json"
    with capture(out, framework="fakefw"):
        _run_fake_agent()
    assert _captured(out) == ["get_order"]
    assert len(backend.get_finished_spans()) == 1  # the caller's backend got the run too

    _run_fake_agent()  # a later run, after the capture
    assert _FakeInstrumentor().is_instrumented_by_opentelemetry
    assert len(backend.get_finished_spans()) == 2
    assert _captured(out) == ["get_order"]  # and nothing more reaches the finished capture


def test_existing_instrumentation_capture_cannot_reach_raises_and_is_kept(
    tmp_path, fakefw, monkeypatch
):
    # smolagents' telemetry docs pass their own provider without making it global.
    from opentelemetry import trace as trace_api

    from tracelint.capture import capture

    provider, backend = _user_tracing()
    monkeypatch.setattr(trace_api, "get_tracer_provider", lambda: trace_api.NoOpTracerProvider())
    _FakeInstrumentor().instrument(tracer_provider=provider)

    with pytest.raises(RuntimeError, match="already instrumented.*set_tracer_provider"):
        with capture(tmp_path / "trace.json", framework="fakefw"):
            _run_fake_agent()

    _run_fake_agent()
    assert _FakeInstrumentor().is_instrumented_by_opentelemetry
    assert len(backend.get_finished_spans()) == 2  # the caller's tracing kept working
