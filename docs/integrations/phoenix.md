# tracelint + Arize Phoenix (OpenInference)

[Arize Phoenix](https://github.com/Arize-ai/phoenix) collects agent traces as **OpenInference**
spans. tracelint reads them directly — it is a *consumer* of the OpenInference telemetry you already
have, no extra SDK in your agent.

## From an exported spans file

```bash
tracelint check spans.json --format openinference
```

## From the Phoenix client (no file needed)

tracelint reads Phoenix dataframe records directly:

```python
import phoenix as px
from tracelint import lint_otel_trace, render_report

records = px.Client().get_spans_dataframe().to_dict("records")
print(render_report(lint_otel_trace(records)))
```

## What tracelint reads

A **TOOL** span becomes a paired tool call + result (`tool.name`, `input.value`, `output.value`); an
OTel `ERROR` status or an exception span event marks a structured tool error (R2a). An **LLM** span
seeds the user turn for provenance. Missing fields cause the relevant rule to *suppress with a
reason* — never a silent pass.

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
