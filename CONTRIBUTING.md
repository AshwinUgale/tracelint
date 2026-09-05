# Contributing to tracelint

Thanks for your interest! tracelint is a focused, honest tool — a **deterministic linter
for agent runs** that reads a tool-calling agent's execution trace and flags structural
bugs with exact evidence, *without a second model judging it*. Contributions that keep it
*trustworthy, deterministic, and simple* are very welcome.

New to the codebase? The `src/tracelint/` modules: `trace.py` (the canonical trace schema)
· `findings.py` / `signatures.py` (the deterministic lint rules) · `provenance.py`
(argument provenance for the hallucinated-arg check) · `injection.py` (the fault injector)
· `scorecard.py` (recovery scoring) · `nondeterminism.py` · `adapters/` (otel / openai /
langfuse) · `report.py` · `cli.py`.

## Found a trace that breaks it?

That's the single most useful report. Open a **[A trace that breaks tracelint](https://github.com/AshwinUgale/tracelint/issues/new?template=trace_that_breaks_it.yml)**
issue and paste the trace, the exact `tracelint` command, and what you expected vs. what you got. A
tiny trace beats a big one, and you can redact values — the shape is what matters. Wrong results
(false positive / false negative) and crashes on a real trace are exactly what we want to see.

## Development setup

```bash
git clone https://github.com/AshwinUgale/tracelint
cd tracelint
pip install -e ".[dev]"          # editable install + pytest & ruff
pytest -q                        # the suite (all green)
tracelint demo                   # keyless validation suite + recovery scorecard (no key)
```

Before opening a PR, run what CI runs:

```bash
ruff check src tests examples    # lint
ruff format src tests examples   # auto-format (CI checks this)
pytest -q                        # unit + validation-suite tests
tracelint demo                   # the keyless self-check must stay green
```

The agent that *generates* traces (`[real-agent]`) and the Langfuse integration
(`[langfuse]`) are optional extras. **The linter and its whole test/validation path run
offline and keyless** — please keep it that way.

## Good first contributions

- **A new lint rule** — the highest-value, smallest surface. Add a deterministic detector
  next to the existing ones (schema-violating call, ignored tool error, hallucinated arg,
  loop, redundant call) in `findings.py`/`signatures.py`, with the exact trace evidence it
  reports. Ideas: unbounded retry without backoff, tool called before its dependency,
  final answer contradicting a tool result.
- **A new trace adapter** — follow `adapters/otel.py` / `openai.py` / `langfuse.py` (e.g.
  OpenInference, LangSmith, or a generic JSON adapter). Never lint a partial trace as if it
  were complete — suppress the checks that need missing fields and say so.
- **A new injected fault** — extend `injection.py` (e.g. partial/streamed tool output,
  reordered results) and score recovery in `scorecard.py`.
- **Docs & examples** — a new example trace with a planted defect, a walkthrough.

Browse issues labeled **`good first issue`** and **`help wanted`**. For anything larger
than a single rule/adapter/fault, open an issue first so we agree on the shape.

## Guidelines

- **Deterministic, no judge.** The linter's premise is that these defects are structurally
  decidable — **no second model ever judges the trace.** A new rule must be a hard,
  reproducible check on the trace, not an LLM call.
- **Candidate, not verdict, where it's heuristic.** Loop / redundancy / hallucinated-arg
  checks have legitimate forms (retry, polling, transformed args) — flag them with evidence
  and tiered confidence, don't assert them as bugs. Allow-list intentional repetition.
- **Fail closed on incomplete traces.** If a trace lacks the fields a rule needs, suppress
  that rule and say so — never emit a clean bill of health on a partial trace.
- **Keep the core dependency-free** (jsonschema + stdlib). Real agent / Langfuse live behind
  extras; the demo and tests run offline.
- **Every rule and public function gets a test** — ideally a planted-defect case in the
  validation suite (correct trace passes, buggy one is caught).
- **Update `CHANGELOG.md`** under the top section for user-visible changes.

## Pull request checklist

- [ ] `pytest -q` passes and new behavior has a test
- [ ] `ruff check src tests examples` and `ruff format …` are clean
- [ ] `tracelint demo` still runs green (offline / keyless)
- [ ] `CHANGELOG.md` updated (for user-visible changes)
- [ ] docs / README touched if the change is user-facing
- [ ] the PR description says *what* and *why* in a sentence or two

## Code of Conduct

This project follows the [Contributor Covenant](CODE_OF_CONDUCT.md). By participating you
agree to uphold it — be kind, be constructive.
