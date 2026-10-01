# tracelint + LangGraph / LangChain

A [LangGraph](https://github.com/langchain-ai/langgraph) agent instrumented with
`openinference-instrumentation-langchain` emits OpenInference spans. tracelint reads them directly.

## Capture a trace in a test

The fastest way in: `tracelint.capture` records a run to a lintable trace by wrapping LangChain's
OpenInference instrumentor for you — the same one LangGraph uses — with no manual provider or export.

```bash
pip install "tracelint[capture-langchain]"
```

```python
# test_agent_trace.py
import json

from tracelint import lint_otel_trace
from tracelint.capture import capture


def test_agent_run_has_no_structural_defects(tmp_path):
    trace = tmp_path / "trace.json"
    with capture(trace, framework="langgraph"):    # "langchain" works too — same instrumentor
        my_graph.invoke({"messages": [("user", "refund order A100")]})   # your agent, unchanged

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
When no LLM span recorded the call (a plain LCEL chain such as `prompt | llm | parser | tool`, or
inputs hidden with `OPENINFERENCE_HIDE_INPUTS`), the arguments are reported as unknown, and the
checks that need them list those calls as not checked rather than failing them.

Reproduce it offline, no API key — from a clone of the [tracelint repo](https://github.com/AshwinUgale/tracelint)
(the bundled example traces ship with the source, not the PyPI wheel):

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
