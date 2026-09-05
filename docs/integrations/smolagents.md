# tracelint + smolagents

[smolagents](https://github.com/huggingface/smolagents)' telemetry tutorial instruments a
`ToolCallingAgent` with OpenInference and ships the spans to Phoenix or Langfuse. tracelint reads
those same spans — no extra SDK in your agent.

## Three steps

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

Reproduce it offline, no API key:

```bash
python examples/lint_smolagents.py
```

(the captured spans live in [`examples/traces/smolagents_trace.json`](../../examples/traces/smolagents_trace.json)).

## Catching real defects

To exercise the behavioral rules (schema violations, tool errors, duplicate side effects), declare a
small `tools.json` with your tools' schemas and `side_effecting` / `idempotent` / `failure_when`, and
pass `--tools tools.json`. See the offline
[`examples/lint_openinference_phoenix.py`](../../examples/lint_openinference_phoenix.py) for a trace
with planted defects that tracelint proves and fails CI on.

## Scope

A compatibility validation on a real trace — illustrative that tracelint reads smolagents' telemetry
with no adapter changes, not a production benchmark.
