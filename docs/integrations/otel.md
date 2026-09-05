# tracelint + OpenTelemetry GenAI (OpenLLMetry / Traceloop)

Agents instrumented with the **OTel GenAI** semantic conventions — e.g.
[OpenLLMetry / Traceloop](https://github.com/traceloop/openllmetry) — identify a tool span by
`gen_ai.operation.name == "execute_tool"` (with `gen_ai.tool.name` and plain `input`/`output`),
rather than the OpenInference `openinference.span.kind` / `tool.name` / `input.value` attributes.
tracelint's OTel adapter handles **both** conventions with one reader.

## Lint the spans you already export

```bash
tracelint check spans.json --format otel
```

(`--format openinference` and `--format otel` share the same underlying adapter; use whichever
matches how your spans are attributed. Mixed exports are read either way.)

## What tracelint reads

An `execute_tool` span → a paired tool call + result; an OTel `ERROR` status or exception event marks
a structured tool error (R2a). `chat` / `completion` spans contribute the LLM turn. Missing fields
suppress the relevant rule with a reason.

## Example (offline, keyless)

- [`examples/lint_traceloop_traces.py`](../../examples/lint_traceloop_traces.py) — an
  OpenLLMetry/Traceloop-shaped export, linted end to end.

## Scope

tracelint consumes OTel GenAI spans as the deterministic verification layer on top of the telemetry
you already collect.
