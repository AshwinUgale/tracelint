# tracelint + Langfuse

[Langfuse](https://github.com/langfuse/langfuse) stores agent traces. tracelint reads them: pull a
trace to a file and lint it like any other input, locally or in CI.

## Pull a trace, then check it

```bash
pip install "tracelint[langfuse]"
export LANGFUSE_PUBLIC_KEY=pk-...   # and LANGFUSE_SECRET_KEY (LANGFUSE_HOST if self-hosted)

tracelint langfuse pull <trace-id>                                    # writes <trace-id>.json
tracelint check <trace-id>.json --format langfuse [--tools tools.json]
```

The pulled file is an ordinary input, so it doubles as a saved regression fixture. `pull` reads the
region-specific `LANGFUSE_*` env vars; `--tool-names a,b` treats those observations as tool calls
(for span-based instrumentation), and `-o` names the output file.

## From a saved Langfuse trace file

```bash
tracelint check trace.json --format langfuse
```

## Advanced: lint in place and write the findings back

```bash
tracelint langfuse check --trace <trace-id> [--tools tools.json] [--write-back]
```

This fetches and lints in one step. `--write-back` posts the findings as Scores on that trace, so the
deterministic verdict shows up next to it in the Langfuse UI; omit it for a read-only lint.

It reads your project config (`[tool.tracelint]` / `tracelint.toml`) for the rules, tools, `fail_on` and
ignores, so it gates like `tracelint check` does. For a baseline (accept today's findings, fail on
new ones), use the `pull` -> `tracelint check --baseline` flow above, since a baseline is keyed to a
committed trace file.

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

tracelint reads Langfuse traces and checks them deterministically; it does not replace Langfuse. It is the verification layer on top of the traces Langfuse already stores.
