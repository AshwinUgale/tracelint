# tracelint + Langflow

[Langflow](https://github.com/langflow-ai/langflow) has a first-class Arize/Phoenix tracer that
instruments its Agent with `LangChainInstrumentor` under the hood
(`langflow/services/tracing/arize_phoenix.py`). So a Langflow agent flow emits LangChain-shaped
OpenInference spans that tracelint reads directly — enabling the tracer is all a user needs.

## Three steps

1. **Enable the Phoenix tracer** in Langflow by pointing it at a Phoenix collector:

   ```bash
   export PHOENIX_COLLECTOR_ENDPOINT=http://localhost:6006   # your Phoenix instance
   ```

   Run your agent flow. (The Langfuse tracer works the same way via `LANGFUSE_*` env vars.)

2. **Export** the spans Phoenix collected:

   ```python
   import phoenix as px
   records = px.Client().get_spans_dataframe().to_dict("records")
   ```

3. **Lint** — tracelint reads the Phoenix dataframe records directly:

   ```python
   from tracelint import lint_otel_trace, render_report
   print(render_report(lint_otel_trace(records)))
   ```

   or, from an exported `spans.json`:

   ```bash
   tracelint check spans.json --format openinference
   ```

## What tracelint sees

On Langflow's shipped **"Simple Agent"** starter flow, run on `gpt-4o-mini` (the agent fetched a URL
via its tool), the real trace lints **clean — 0 findings, exit 0**. Because Langflow's tracer is
`LangChainInstrumentor`, the same shared adapter that handles LangChain/LangGraph handles Langflow —
**no Langflow-specific code**.

Reproduce it offline, no API key:

```bash
python examples/lint_langflow.py
```

(captured spans: [`examples/traces/langflow_trace.json`](../../examples/traces/langflow_trace.json)).

## Catching real defects

Declare a `tools.json` (schemas + `side_effecting` / `idempotent` / `failure_when`) and pass
`--tools tools.json` to light up the behavioral rules on the tools your flow calls. Bootstrap it
straight from the trace — `tracelint init spans.json --format openinference -o tools.json` discovers
the tools and their schemas, leaving only the behavior to fill in.

## Scope

A compatibility validation on a real trace — illustrative that tracelint reads Langflow's
Phoenix/OpenInference telemetry with no adapter changes, not a production benchmark.
