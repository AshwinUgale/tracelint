# tracelint

**ESLint/pytest for what your agent actually did.** `tracelint` is a deterministic, judge-free
static analyzer for the execution traces of tool-calling agents. It reads a trace and reports
structural defects — schema-violating tool calls, ignored tool errors, hallucinated arguments,
loops, and redundant calls — each with the exact trace lines as evidence. No second model judges
the trace; the checks are structurally decidable, so they are reproducible and cheap.

Model-as-judge detection of these defects is unreliable (published trace-error benchmarks show
low localization accuracy). Many of these defects are *structurally decidable* and need no judge —
that is the entire premise of this tool.

## Limitations (read first)

1. Deterministic rules catch **structural** defects, not whether the final answer was correct.
2. Hallucinated-argument, loop, and redundant-call findings are **candidates** unless
   structurally proven — legitimate value transforms and intentional retries can trip them; each
   is shown with its evidence for human review, never asserted as a verdict. High-confidence
   hallucination detection requires the tool schema to declare field origins.
3. The recovery scorecard needs labeled task outcomes (success oracles); without them it measures
   **behavioral** recovery only ("did not crash"), which is a weaker claim than correctness.
4. A trace is only as complete as its instrumentation. A rule whose required field is missing is
   **suppressed with a stated reason** — `tracelint` never lints a partial trace as if complete.

## Status

Under active construction, built in phases. See
[`agent-trace-linter/progress/STATUS.md`](https://github.com/AshwinUgale) in the roadmap repo for
the milestone checklist. Phase 0 (canonical trace schema, uniform finding shape, tool registry,
and the fail-closed suppression foundation) is in place; the rules land in subsequent phases.

## Development

```bash
python -m pytest
ruff check src tests
```

The core is dependency-light (`jsonschema` + stdlib); a real trace-generating agent lives behind
the opt-in `[real-agent]` extra and is never part of the deterministic linter. Python 3.10–3.12.
