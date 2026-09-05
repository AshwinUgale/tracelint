# tracelint + LangGraph / LangChain

A [LangGraph](https://github.com/langchain-ai/langgraph) agent instrumented with
`openinference-instrumentation-langchain` emits OpenInference spans. tracelint reads them directly.

## Three steps

1. **Instrument** with the LangChain OpenInference instrumentor:

   ```python
   from openinference.instrumentation.langchain import LangChainInstrumentor
   LangChainInstrumentor().instrument(tracer_provider=provider)
   ```

2. **Export** the collected spans to `spans.json` (or read them from Phoenix / your OTLP backend).

3. **Lint**:

   ```bash
   tracelint check spans.json --format openinference
   ```

## What tracelint sees

On a real `gpt-4o-mini` `create_react_agent` run, the trace lints **clean — 0 findings, exit 0**.

LangChain's instrumentation places the tool call's structured arguments on the **LLM span's**
`tool_calls`, while the TOOL span records only a bare scalar input. tracelint's shared OpenInference
adapter recovers the real arguments from the originating LLM tool_call, so a valid call is **not**
mis-flagged as malformed (R6) or schema-violating (R1). This works with no LangGraph-specific code.

Reproduce it offline, no API key:

```bash
python examples/lint_langgraph.py
```

(captured spans: [`examples/traces/langgraph_trace.json`](../../examples/traces/langgraph_trace.json)).

## Catching real defects

Declare a `tools.json` (schemas + `side_effecting` / `idempotent` / `failure_when`) and pass
`--tools tools.json` to light up the behavioral rules on your own tools. Bootstrap it straight from
the trace — `tracelint init spans.json --format openinference -o tools.json` discovers the tools and
their schemas, leaving only the behavior to fill in.

## Scope

A compatibility validation on a real trace — illustrative that tracelint reads LangChain/LangGraph
telemetry with no adapter changes, not a production benchmark.
