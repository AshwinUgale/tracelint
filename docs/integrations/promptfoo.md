# tracelint + promptfoo

[promptfoo](https://promptfoo.dev) runs evals from a YAML config and checks each output with
assertions. tracelint plugs in as a **Python assertion**: your deterministic trace checks become a
pass/fail gate in the same suite — same rules as `tracelint check`, **no judge and no tokens**.

promptfoo is a Node tool that just calls a Python `get_assert(output, context)` function, so there's
**nothing extra to pip-install** — `tracelint` itself is enough.

## Zero-Python: point promptfoo at the shipped assertion

tracelint scores a *trace*, so tell the assertion where the run's trace is (a `vars` entry) and
configure the check in the assertion's `config`:

```yaml
# promptfooconfig.yaml
tests:
  - vars:
      tracelint_trace: traces/run-42.json      # path to the trace this case produced
    assert:
      - type: python
        value: file://$(python -c "import tracelint.integrations.promptfoo as m; print(m.__file__)")
        config:
          tools: tools.json
          fail_on: hard_defect                  # or hard_event / candidate
          fmt: native                           # any `tracelint check --format`
```

Prefer a stable path? Drop a one-line file next to your config and reference it instead:

```python
# tracelint_assert.py
from tracelint.integrations.promptfoo import get_assert  # noqa: F401
```

```yaml
      - type: python
        value: file://tracelint_assert.py
        config: { tools: tools.json }
```

## Where the trace comes from

The assertion looks, in order:

1. `config.trace` — an explicit path (or a `Trace`), set right on the assertion;
2. `vars.<trace_var>` — a `vars` entry, `tracelint_trace` by default (rename with `config.trace_var`);
3. the `output` — when the provider's output is itself a path to a trace file.

A case with no trace, or a path that won't load, **fails that assertion with the reason recorded** —
it never errors the whole eval.

## Pass / fail

`pass` is exactly `tracelint check`'s clean exit under `fail_on`: a **hard defect** (e.g. R1 schema
violation, R2b error-value reuse) always fails; `fail_on: hard_event` or `candidate` opts the gate
down. `score` is `1.0` on a pass and `0.0` on a fail — a deterministic verdict — and `reason` lists
the findings with their trace steps. `tools`, `rules`, `fmt` and `fail_on` mirror the CLI.

## Your own wrapper

Need logic promptfoo's `config` can't express? Call the explicit core from your own `get_assert`:

```python
from tracelint.integrations.promptfoo import assert_trace

def get_assert(output, context):
    return assert_trace(output, context, tools="tools.json", rules=["R1", "R2b"], fail_on="hard_event")
```

`assert_trace` returns the same `{pass, score, reason}` GradingResult. The scoring itself is the
dependency-free [`score_trace`](../../src/tracelint/integrations/scoring.py), shared with the
[DeepEval metric](deepeval.md).

## Scope

tracelint checks what's *mechanically decidable* from the trace — it doesn't judge whether the
answer was correct (that's your other promptfoo assertions). Behavioral rules (R1 schema, R2b/R8/R9
side-effect rules) need a small `tools.json`; most other rules run keyless. See the
[rule reference](../rules.md) for what each rule checks.
