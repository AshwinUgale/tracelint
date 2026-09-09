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
