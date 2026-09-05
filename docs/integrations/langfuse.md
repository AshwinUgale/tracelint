# tracelint + Langfuse

[Langfuse](https://github.com/langfuse/langfuse) stores agent traces. tracelint has the deepest
integration of any platform here: it can **fetch a trace from Langfuse, lint it, and write the
findings back as Langfuse Scores** — so the deterministic verdict shows up next to the trace in the
Langfuse UI.

## Native fetch + write-back

```bash
pip install "tracelint[langfuse]"
export LANGFUSE_PUBLIC_KEY=pk-...   # and LANGFUSE_SECRET_KEY (LANGFUSE_HOST if self-hosted)

tracelint langfuse check --trace <trace-id> [--tools tools.json] [--write-back]
```

`--write-back` posts the findings as Scores on that trace; omit it for a read-only lint. Reads the
region-specific `LANGFUSE_*` env vars.

## From a saved Langfuse trace file

```bash
tracelint check trace.json --format langfuse
```

## Examples

- [`examples/langfuse_cookbook.py`](../../examples/langfuse_cookbook.py) — offline & keyless on a
  bundled Langfuse-shaped trace, plus the live fetch + score-push flow.
- [`examples/lint_langfuse_traces.py`](../../examples/lint_langfuse_traces.py) — read and lint
  Langfuse traces.
- [`examples/langfuse_generate_and_lint.py`](../../examples/langfuse_generate_and_lint.py) — a real
  agent → Langfuse → tracelint round-trip, validating the adapter on the actual bytes Langfuse
  returns.

## Note on schemas

Langfuse traces rarely carry tool JSON Schemas, so bring a `tools.json` (`--tools`) to light up R1
and the behavioral rules; most other rules run without it.

## Scope

tracelint consumes Langfuse traces and augments them with a deterministic score; it does not replace
Langfuse. It is the verification layer on top of the traces Langfuse already stores.
