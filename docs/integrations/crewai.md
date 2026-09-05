# tracelint + CrewAI

A [CrewAI](https://github.com/crewAIInc/crewAI) crew instrumented with
`openinference-instrumentation-crewai` emits agent / task / tool spans. tracelint reads them.

## Three steps

1. **Instrument** with the CrewAI OpenInference instrumentor:

   ```python
   from openinference.instrumentation.crewai import CrewAIInstrumentor
   CrewAIInstrumentor().instrument(tracer_provider=provider)
   ```

2. **Export** the collected spans to `spans.json` (or read them from Phoenix).

3. **Lint**:

   ```bash
   tracelint check spans.json --format openinference --include-candidates
   ```

## What tracelint sees

On a real `gpt-4o-mini` crew run (a support agent refunding an order), the tool call and result read
cleanly — the TOOL span records canonical JSON arguments, so there are **no false hard defects**
(exit 0).

One **candidate** is raised: CrewAI's instrumentation traces the agent / task / tool but **not the
LLM turn**, so no LLM span carries the user's request. With no observed origin for the argument, R3
raises a *candidate* (possible-false-positive). It **never fails CI**, and it clears the moment the
trace also includes an LLM span with the user turn (e.g. by adding an LLM instrumentor alongside the
CrewAI one). It is a coverage characteristic of the instrumentation, not a defect in the agent.

Reproduce it offline, no API key — from a clone of the [tracelint repo](https://github.com/AshwinUgale/tracelint)
(the bundled example traces ship with the source, not the PyPI wheel):

```bash
python examples/lint_crewai.py
```

(captured spans: [`examples/traces/crewai_trace.json`](../../examples/traces/crewai_trace.json)).

## Bonus: schemas are in the trace

CrewAI's TOOL span carries `tool.parameters` — the tool's JSON schema — so schema-based checks (R1)
can be driven from the trace itself. `tracelint init spans.json --format openinference -o tools.json`
discovers those schemas into a starter contract; add `side_effecting` / `idempotent` / `failure_when`
to it for the behavioral rules.

## Scope

A compatibility validation on a real trace — illustrative that tracelint reads CrewAI telemetry with
no adapter changes, not a production benchmark.
