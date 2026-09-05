# tracelint + the OpenInference / Langfuse ecosystem

tracelint is a deterministic, judge-free linter for tool-calling agent traces. It does **not**
require its own SDK in your agent: it reads the OpenTelemetry / **OpenInference** spans your agent
already emits and reports structural defects (schema violations, tool errors, error-value reuse,
malformed calls, duplicate side effects, loops) with evidence and a CI exit code — **no model in the
loop**.

Because it consumes the telemetry standard rather than instrumenting each framework, one shared
adapter reaches the whole ecosystem. The frameworks below were each validated on a **real
`gpt-4o-mini` trace**, instrumented with that framework's own stock OpenInference package, and lint
correctly with **zero framework-specific code**.

| Framework | Instrumentation | Result on a real trace | Example |
|---|---|---|---|
| [smolagents](smolagents.md) | `openinference-instrumentation-smolagents` | clean, no config | [`examples/lint_smolagents.py`](../../examples/lint_smolagents.py) |
| [LangGraph / LangChain](langgraph.md) | `openinference-instrumentation-langchain` | clean, no config | [`examples/lint_langgraph.py`](../../examples/lint_langgraph.py) |
| [CrewAI](crewai.md) | `openinference-instrumentation-crewai` | clean (one non-CI-failing candidate) | [`examples/lint_crewai.py`](../../examples/lint_crewai.py) |
| [Langflow](langflow.md) | Phoenix tracer (= `LangChainInstrumentor`) | clean, no config | [`examples/lint_langflow.py`](../../examples/lint_langflow.py) |

Each example is **offline and keyless** — it ships the real captured spans and just lints them, so
you can reproduce the result with no API key.

## The two ingestion paths

**OpenInference / OpenTelemetry** (Arize Phoenix, OpenLLMetry, LangSmith export, OTLP) — point
tracelint at the spans you already collect:

```bash
tracelint check spans.json --format openinference
```

If you use Arize Phoenix, you can lint the spans directly from the client dataframe — see
[`examples/lint_openinference_phoenix.py`](../../examples/lint_openinference_phoenix.py) and
[`examples/lint_phoenix_traces.py`](../../examples/lint_phoenix_traces.py).

**Langfuse** — tracelint has a native integration that fetches a trace and can write findings back as
Langfuse **Scores**:

```bash
tracelint langfuse check --trace <trace-id> --write-back
```

See [`examples/lint_langfuse_traces.py`](../../examples/lint_langfuse_traces.py) and
[`examples/langfuse_cookbook.py`](../../examples/langfuse_cookbook.py).

## What tracelint proves vs. suggests

- **hard_defect** (fails CI, exit 2): schema violation (R1), error-value reused by a side-effecting
  call (R2b), malformed arguments (R6).
- **hard_event** (a certain fact, never fails CI): a tool returned an error (R2a), a non-idempotent
  side effect repeated after success (R8).
- **candidate** (heuristic, never fails CI): hallucinated arg (R3), loop (R4), redundant call (R5),
  unknown tool (R7).

When a rule can't run (missing data) it is **suppressed with a reason** — never a silent pass — and
tracelint reports per-rule verification coverage.

## Scope

These are **compatibility validations** on real traces — illustrative that tracelint reads each
framework's telemetry with no adapter changes, not production benchmarks or endorsements. Behavioral
rules (R1 schema, R2b/R8 side-effect rules) need a small `tools.json` declaring tool schemas and
`side_effecting` / `idempotent` / `failure_when`; most other rules run keyless.
