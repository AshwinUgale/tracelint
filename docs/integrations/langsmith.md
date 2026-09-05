# tracelint + LangSmith

[LangSmith](https://smith.langchain.com) records a trace as a **run tree** — a root run with nested
`child_runs`. tracelint reads that tree directly.

## Lint an exported run tree

Export the run (tree) as JSON, then:

```bash
tracelint check run_tree.json --format langsmith
```

## What tracelint reads

- A run with `run_type == "tool"` → a tool call (arguments from `inputs`) + its result (from
  `outputs`).
- LLM/chat runs contribute assistant text.
- A run whose result status is missing or unclassifiable is left `unknown`, so the affected rules
  **suppress with a reason** rather than pretend the trace is complete.

The mapping is kept deliberately small and faithful — tracelint reports only what the run tree
actually proves.

## Getting the run tree

Fetch it with the LangSmith SDK (`langsmith.Client().read_run(<id>, load_child_runs=True)`) and dump
it to JSON, or use an existing export. (There is no bundled example for this path yet — the adapter
is covered by `tests/test_adapter_langsmith.py`.)

## Note on schemas

LangSmith runs rarely embed tool JSON Schemas, so pass a `tools.json` (`--tools`) to enable R1 and
the behavioral rules; the ground-truth-free rules run without it.

## Scope

tracelint consumes a LangSmith run tree as the deterministic verification layer; it does not replace
LangSmith.
