# tracelint

**ESLint/pytest for what your agent actually did.** `tracelint` is a deterministic, judge-free
static analyzer for the execution traces of tool-calling agents. It reads a trace and reports
structural defects — schema-violating tool calls, ignored tool errors, hallucinated arguments,
loops, and redundant calls — each with the exact trace lines as evidence, and returns a CI exit
code. It also ships a fault injector and a per-fault recovery scorecard.

Model-as-judge detection of these defects is unreliable (published trace-error benchmarks show low
localization accuracy). Many of these defects are *structurally decidable* and need no judge — that
is the entire premise of this tool. No second model ever judges the trace.

## Limitations (read first)

1. Deterministic rules catch **structural** defects, not whether the final answer was correct.
2. Hallucinated-argument, loop, and redundant-call findings are **candidates** unless structurally
   proven — legitimate value transforms and intentional retries can trip them; each is shown with
   its evidence for human review, never asserted as a verdict. High-confidence hallucination
   detection requires the tool schema to declare field origins (`x-value-origin`).
3. The recovery scorecard needs labeled task outcomes (success oracles); without them it measures
   **behavioral** recovery only ("did not crash"), a weaker claim than correctness.
4. A trace is only as complete as its instrumentation. A rule whose required field is missing is
   **suppressed with a stated reason** — `tracelint` never lints a partial trace as if complete.

## Quick start

The demo runs a keyless validation suite and a recovery scorecard end to end — no API key, no
model download:

```bash
pip install tracelint
tracelint demo --html demo.html
```

Lint a trace in CI:

```bash
tracelint check ./trace.json --tools ./tools.json     # exit 2 on a hard_defect
```

Exit codes: `0` clean · `2` a structurally-provable defect (`hard_defect`) · `3` an input error.
Heuristic candidates never fail CI on their own; suppressions are disclosed but are not defects.

## The rules

| Rule | Finding | Tiers |
|------|---------|-------|
| R1 | schema violation — args fail the tool's JSON Schema | `hard_defect` |
| R2a | tool returned an error | `hard_event` (structured signal) / `candidate` (heuristic) |
| R2b | an errored result's value reused by a later side-effecting call | `hard_defect` / `candidate` |
| R3 | hallucinated argument — value not derivable from provenance | `candidate`; `hard_defect` if the field is annotated `provided` |
| R4 | loop — N identical no-progress calls (polls/retries excluded) | `candidate` |
| R5 | redundant call — identical call + identical result, no mutation between | `candidate` |

`hard_event` and `hard_defect` are orthogonal to the finding kind: a tool-error event is a
`hard_event` from a structured status field but a `candidate` from an exception-like string in
free-form content.

## Input format

A trace is a JSON object (`.json`, or `.jsonl` for many):

```json
{
  "run_id": "run-1",
  "steps": [
    {"type": "message", "role": "user", "content": "cancel order 4521 if it hasn't shipped"},
    {"type": "tool_call", "call_id": "c1", "name": "get_order_status", "args": {"order_id": "4521"}},
    {"type": "tool_result", "call_id": "c1", "content": {"status": "processing"}, "status": "ok"},
    {"type": "tool_call", "call_id": "c2", "name": "cancel_order",
     "args": {"order_id": "4521", "reason": "not_shipped"}}
  ],
  "final": "Order 4521 has been cancelled."
}
```

`tools.json` supplies the ground truth the rules check against:

```json
{
  "tools": {
    "cancel_order": {
      "schema": {"type": "object", "properties": {"order_id": {"type": "string"}},
                 "required": ["order_id"]},
      "metadata": {"side_effecting": true}
    }
  }
}
```

An OpenAI adapter (`tracelint.adapters.from_openai_messages`) normalizes OpenAI chat message lists
into this schema; more adapters are future work.

## Recovery scorecard

Measure how an agent behaves under injected faults, scored against deterministic success oracles:

```bash
tracelint scorecard --demo --faults timeout,error,rate_limit --runs 5
```

The baseline must satisfy the oracle first (else recovery is not measured). Each fault type reports
a correctness-recovery rate with a Wilson confidence interval; with no oracle it falls back to
behavioral recovery, labeled as weaker.

## Library

```python
from tracelint import lint_trace, default_rules, Trace, ToolRegistry

trace = Trace.load("trace.json")
registry = ToolRegistry.load("tools.json")
report = lint_trace(trace, default_rules(), registry)
print(report.exit_code)          # 0 or 2
for f in report.active_findings:
    print(f.rule, f.tier.value, f.summary)
```

## Development

```bash
python -m pytest
ruff check src tests
```

The core is dependency-light (`jsonschema` + stdlib) and the whole test suite is deterministic and
offline. A real OpenAI trace-generating agent lives behind the opt-in `[real-agent]` extra and is
never part of the linter. Python 3.10–3.12.
