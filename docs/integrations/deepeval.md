# tracelint + DeepEval

[DeepEval](https://deepeval.com) is an LLM-eval framework: you build `LLMTestCase`s and score them
with metrics. tracelint exposes its deterministic trace checks as one such metric, so a team already
running DeepEval gets a **structural-reliability check in the same suite** — same rules, same
pass/fail gate as `tracelint check`, **no judge and no tokens**.

tracelint scores an agent's *trace* (its tool calls and results), not an LLM's text output, so the
metric either checks a trace you bind to it or one carried on the test case.

```bash
pip install "tracelint[deepeval]"
```

## Score one trace

```python
from deepeval.test_case import LLMTestCase
from tracelint.integrations.deepeval import TracelintMetric

metric = TracelintMetric(trace="run.json", fmt="native", tools="tools.json")
metric.measure(LLMTestCase(input="...", actual_output="..."))
assert metric.is_successful()        # False if the trace has a hard defect
print(metric.score, metric.reason)   # 1.0 / 0.0, and the findings
```

`trace=` is a `tracelint.Trace` or a path to a trace file read with `fmt` (any `tracelint check
--format`: `native`, `openinference`, `otel`, `langfuse`, …). `tools=` is a `tools.json` path or a
`ToolRegistry`; `rules=` selects rule ids (default: all); `fail_on=` matches `--fail-on`.

## Score many traces in a run

The idiomatic DeepEval flow is one metric over many cases — carry each run's trace on the test case
under `additional_metadata`:

```python
from deepeval import evaluate
from deepeval.test_case import LLMTestCase
from tracelint.integrations.deepeval import TracelintMetric

cases = [
    LLMTestCase(input=q, actual_output=a, additional_metadata={"tracelint_trace": path})
    for q, a, path in runs
]
evaluate(cases, metrics=[TracelintMetric(tools="tools.json")])
```

The metric reads `additional_metadata["tracelint_trace"]` (override the key with `trace_key=`). A
case with no trace, or a path that won't load, fails that case with the reason recorded in
`metric.error` — it never crashes the run.

## Pass / fail

`success` is exactly `tracelint check`'s clean exit under the chosen `fail_on`: a **hard defect**
(e.g. R1 schema violation, R2b error-value reuse) always fails; `fail_on="hard_event"` or
`"candidate"` opts the gate down to those tiers. `score` is `1.0` on a pass and `0.0` on a fail — a
deterministic verdict, not a probability — and `reason` lists the findings with their trace steps.

## Pure core, no SDK

The scoring logic is a dependency-free function you can call without DeepEval (e.g. from another
harness):

```python
from tracelint.integrations.deepeval import score_trace
from tracelint import load_source

result = score_trace(load_source("run.json", "native")[0], rules=None)
print(result.success, result.score, result.reason)
```

Only `TracelintMetric` needs the DeepEval SDK; it is built on first use, so importing
`tracelint.integrations.deepeval` never requires DeepEval to be installed.

## Scope

tracelint checks what's *mechanically decidable* from the trace — it does not judge whether the
agent's answer was correct (that's what your other DeepEval metrics are for). Behavioral rules (R1
schema, R2b/R8/R9 side-effect rules) need a small `tools.json`; most other rules run keyless. See
the [rule reference](../rules.md) for what each rule checks.
