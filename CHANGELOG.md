# Changelog

All notable changes to tracelint are documented here. This project adheres to
[Semantic Versioning](https://semver.org) (pre-1.0: minor versions may introduce
additive features; the public API is not yet frozen).

## [Unreleased]

- Feature: tools can declare `metadata.failure_when` — a JSON-pointer failure predicate
  (`{"pointer": "/status", "in": ["declined", "failed"]}`) — so a domain failure returned as a
  transport success (HTTP 200 with `{"status": "declined"}`) is caught structurally by R2, feeding
  R2a (hard event) and R2b (hard defect on reuse into a side-effecting call). A side-effecting tool
  with no predicate and an unclassifiable result is now **suppressed with a reason** instead of
  passing silently. Raised in review.
- R5 now discloses when a tool absent from the registry ran between two identical calls (its
  side-effect status is unverifiable) rather than silently assuming it was inert.

## [0.3.3]

- Fix: the OpenTelemetry/OpenInference adapter now seeds the opening user/system turn from
  `llm.input_messages` (the OpenInference field holding what the model was asked). Without it,
  provenance had no record of the user's request and reported every string argument as an
  underivable value — a wall of false R3 candidates on real traces.
- Fix: read the span status from the shapes a real export uses — the OTel SDK's nested
  `{"status": {"status_code": "ERROR"}}` and OTLP-JSON's `{"code": "STATUS_CODE_ERROR"}` / numeric
  `2` — not only a flat `status_code`. A real SDK-exported tool error was missed when its failure
  wasn't also echoed in the output payload. Both found by linting genuinely SDK-exported spans.

## [0.3.2]

- Fix: the OpenTelemetry/OpenInference adapter now reads the shape a real Phoenix user gets from
  `px.Client().get_spans_dataframe().to_dict("records")` — required columns at top level and
  attributes flattened into `attributes.*` columns. Before this, a dataframe-record span was
  recognized by kind but read with empty args and no output. Verified against a real Phoenix trace.

## [0.3.1]

- Fix: the OpenTelemetry/OpenInference adapter now recognizes Arize **Phoenix's own trace export**,
  which records the span kind as a top-level `span_kind` field rather than the
  `openinference.span.kind` attribute. Before this, a real Phoenix export was reduced to zero tool
  calls and every rule silently suppressed. Found by running `check --format openinference` against
  a real Phoenix trace.

## [0.3.0]

- `tracelint check --format {openinference,otel,openai,langfuse}` reads provider trace exports
  directly — no manual conversion to the tracelint schema. Multi-trace inputs (`.jsonl`, a JSON
  array, or an OTLP export with several `trace_id`s) fan out to one report each.
- New public API: `load_source`, `lint_otel_trace`, `lint_openai_trace`, `lint_langfuse_trace`,
  and `SUPPORTED_FORMATS`.
- New keyless example `examples/lint_openinference_phoenix.py` — lint Arize Phoenix-shaped
  OpenInference spans end to end.
- `__version__` is now read from the installed package metadata, so it can no longer drift from
  `pyproject.toml` (it previously reported `0.1.0`).

## [0.2.1]

- Deterministic agent-trace linter: flags schema-violating tool calls, ignored tool
  errors, hallucinated arguments, loops, and redundant calls — each with the exact trace
  lines as evidence and a CI exit code. No model ever judges the trace.
- Canonical trace schema + adapters for OpenTelemetry / OpenInference, the OpenAI SDK,
  and Langfuse.
- Fault injector + per-fault recovery scorecard.
- Keyless `tracelint demo` validation suite (one planted instance of every defect, clean
  controls, and legitimate-but-suspicious cases) with a live HTML report.
