# tracelint + smolagents

[smolagents](https://github.com/huggingface/smolagents)' telemetry tutorial instruments a
`ToolCallingAgent` with OpenInference and ships the spans to Phoenix or Langfuse. tracelint reads
those same spans — no extra SDK in your agent.

## Capture a trace in a test

The fastest way in: `tracelint.capture` records a run to a lintable trace by wrapping smolagents'
OpenInference instrumentor for you — no manual provider, no export step.

```bash
pip install "tracelint[capture-smolagents]"
```

```python
# test_agent_trace.py
import json

from tracelint import lint_otel_trace
from tracelint.capture import capture


def test_agent_run_has_no_structural_defects(tmp_path):
    trace = tmp_path / "trace.json"
    with capture(trace, framework="smolagents"):
        run_my_agent("refund order A100")          # your agent under test, unchanged

    report = lint_otel_trace(json.loads(trace.read_text()))
    assert not report.has_hard_defect              # a provable defect fails the test
```

Or capture in one CI job and lint in the next as a shell step:

```bash
tracelint check trace.json --format openinference
```

## Or wire the instrumentor yourself

Already run the OpenInference instrumentor and export spans elsewhere? Lint those directly — capture
is only a convenience over this path:

1. **Instrument** exactly as the smolagents telemetry docs show:

   ```python
   from openinference.instrumentation.smolagents import SmolagentsInstrumentor
   SmolagentsInstrumentor().instrument(tracer_provider=provider)
   ```

2. **Export** the spans your OTel provider collected to `spans.json` (or read them from Phoenix).

3. **Lint**:

   ```bash
   tracelint check spans.json --format openinference
   ```

## What tracelint sees

On a real `gpt-4o-mini` `ToolCallingAgent` run (a support agent asked to refund an order), the
trace lints **clean — 0 findings, exit 0**. The tool call, the tool result, and the user turn (which
smolagents records in OpenInference's nested *content-parts* message shape) are all read correctly,
with no false positives.

Reproduce it offline, no API key — from a clone of the [tracelint repo](https://github.com/AshwinUgale/tracelint)
(the bundled example traces ship with the source, not the PyPI wheel):

```bash
python examples/lint_smolagents.py
```

(the captured spans live in [`examples/traces/smolagents_trace.json`](../../examples/traces/smolagents_trace.json)).

## Catching real defects

To exercise the behavioral rules (schema violations, tool errors, duplicate side effects), declare a
small `tools.json` with your tools' schemas and `side_effecting` / `idempotent` / `failure_when`, and
pass `--tools tools.json`. Bootstrap it with `tracelint init spans.json --format openinference -o
tools.json` — smolagents traces carry the tool schemas, so they're discovered for you. See the offline
[`examples/lint_openinference_phoenix.py`](../../examples/lint_openinference_phoenix.py) for a trace
with planted defects that tracelint proves and fails CI on.

## Scope

A compatibility validation on a real trace — illustrative that tracelint reads smolagents' telemetry
with no adapter changes, not a production benchmark.
