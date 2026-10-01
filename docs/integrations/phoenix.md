# tracelint + Arize Phoenix (OpenInference)

[Arize Phoenix](https://github.com/Arize-ai/phoenix) collects agent traces as **OpenInference**
spans. tracelint reads them directly — it is a *consumer* of the OpenInference telemetry you already
have, no extra SDK in your agent.

## From the Phoenix client (no file needed)

tracelint reads Phoenix dataframe records directly. A project holds many runs, so use
`lint_otel_traces`, which lints each trace separately:

```python
from phoenix.client import Client
from tracelint import lint_otel_traces, render_report

spans = Client().spans.get_spans_dataframe(project_name="my-agent")
for report in lint_otel_traces(spans.to_dict("records")):   # one report per run
    print(render_report(report))
```

(`lint_otel_trace`, singular, is for one run's spans; given spans from several traces it raises
rather than merging them.)

## From an exported spans file

```python
spans.to_json("spans.json", orient="records", date_format="iso")
# or one span per line, e.g. for a CI step that lints traces/*.jsonl:
spans.to_json("spans.jsonl", orient="records", lines=True, date_format="iso")
```

```bash
tracelint check spans.json --format openinference   # one report per trace
```

## What tracelint reads

A **TOOL** span becomes a paired tool call + result (`tool.name`, `input.value`, `output.value`); an
OTel `ERROR` status or an exception span event marks a structured tool error (R2a). An **LLM** span
seeds the user turn for provenance. Missing fields cause the relevant rule to *suppress with a
reason* — never a silent pass.

The Phoenix dataframe returns message attributes such as `llm.input_messages` as nested lists, and
LangChain / LangGraph record each tool result as a serialized `ToolMessage`; tracelint reads both
(validated on a real LangGraph 1.2 run exported from Phoenix — see
[`examples/traces/langgraph_phoenix_trace.json`](../../examples/traces/langgraph_phoenix_trace.json)).

## Examples (offline, keyless)

- [`examples/lint_openinference_phoenix.py`](../../examples/lint_openinference_phoenix.py) — a
  Phoenix-shaped span export with planted defects; lints schema-free, then with a `tools.json` so R1
  proves a schema violation and the process exits `2`.
- [`examples/lint_phoenix_traces.py`](../../examples/lint_phoenix_traces.py) — the dataframe-record
  shape a real Phoenix client returns.
- Every framework example under [`docs/integrations/`](README.md) flows through this same
  OpenInference path.

## Scope

tracelint consumes OpenInference; it does not replace Phoenix. Use it as the deterministic
verification layer on top of the traces Phoenix already stores.
