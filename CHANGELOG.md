# Changelog

All notable changes to tracelint are documented here. This project adheres to
[Semantic Versioning](https://semver.org) (pre-1.0: minor versions may introduce
additive features; the public API is not yet frozen).

## [Unreleased]

- **HTML report is now a trace view.** `tracelint check --html report.html` renders each linted run
  as the agent's **step timeline** (each step that a finding touches is marked inline with its
  rule + tier) beside **finding cards** (tier, rule, summary, evidence) and a **verification
  coverage** bar — instead of a bare findings table. `render_html` gained a `traces=` parameter
  (pass alongside `reports=`); without it the compact table still renders. Still a single
  self-contained file — inline CSS, **no scripts, no external resources**.

- **Langfuse integration — `tracelint langfuse check --trace <id>`.** tracelint now runs *inside*
  the platform you already use: it fetches a trace from Langfuse, lints it, and (with
  `--write-back`) writes the verdict back as **Scores** — a trace-level `tracelint.passed`
  (BOOLEAN) and `tracelint.hard_defects` (NUMERIC), plus each *certain* finding (`hard_defect` /
  `hard_event`) attached to the **exact offending observation** with the evidence in the comment.
  Read-only by default (prints the score plan); candidates are review-only and never written.
  Scores are keyed by stable finding fingerprints, so a re-run updates in place instead of
  duplicating. New `integrations/` layer, kept separate from the pure adapters; needs
  `pip install "tracelint[langfuse]"` (v3 SDK). Reframes the pitch: *add deterministic structural
  checks to your Langfuse traces.*

- **Source identity on canonical steps** (`SourceRef`): adapters can now record where a step came
  from in its origin platform — `provider` + `trace_id` / `span_id` / `observation_id` — so an
  integration can attach a finding back to the exact offending record (a Langfuse observation, an
  OTel/Phoenix span). Optional and absent by default; the Langfuse adapter populates it today. The
  rule engine never reads it, keeping provider knowledge out of the deterministic core. Groundwork
  for observability-platform write-back.
- **Stable finding fingerprints** (`tracelint.identity.finding_fingerprint`): a deterministic id
  from a finding's rule, kind, scope, and evidence locations — independent of output format. SARIF's
  `partialFingerprints` now derive from it, and it will key idempotent write-back (update, not
  duplicate, on re-run).

- **SARIF output** for GitHub code scanning (`tracelint check --sarif out.sarif`, and a `sarif:`
  input on the GitHub Action). Emits a SARIF 2.1.0 log so findings appear in the repo's
  *Security → Code scanning* tab and as inline PR annotations. Tiers map to SARIF levels
  (`hard_defect` → `error`, `hard_event` → `warning`, `candidate` → `note`); suppressions are not
  results; each result carries stable `partialFingerprints` and the trace `step_indices`. The file
  is written before the exit-`2` gate, so an `if: always()` `upload-sarif` step runs even on a
  defect. Library entry point: `tracelint.to_sarif(reports, tool_version=..., uris=...)`. (#13)

- New rule **R8 — duplicate side effect**: flags a non-idempotent side-effecting tool called again
  with equivalent arguments when the first call did **not** fail (the double-charge). `hard_event`
  when the first call succeeded, `candidate` when its outcome is unknown; a repeat after a genuine
  failure is a legitimate retry and is never flagged. Uses only the existing `side_effecting` /
  `idempotent` metadata, and reports an *event*, so it never fails CI on its own.

- Tool Contracts: `ToolContract` presents a tool's declared metadata as one coherent view — `args`
  (schema), `effects` (`side_effecting` / `idempotent` / …), `failure` (`failure_when`), and
  `provenance` (`x-value-origin`) — via `registry.contract_for(name)` / `registry.contracts()`, with
  `.describe()` and `.to_dict()`. Presentation only: no new keys and no behaviour change (it reads the
  same `ToolSpec` the rules already use). `FailurePredicate.summary()` renders a failure contract
  statically. See `docs/tool-contracts.md`.
- Verification coverage: each report now carries a per-rule `coverage` — how many units a rule could
  actually evaluate vs. abstain on (e.g. `R1  1/2 tool calls`, `R2a  1/2 tool results`), shown in the
  text report and `to_dict()`. Rules opt in via `Rule.coverage()`; R1 (schema availability) and R2a
  (structurally-classifiable results) report today, and a whole-rule suppression reads as `0 / total`.
  This makes "what portion of this run was actually verifiable?" a number you can watch — the reason a
  clean report is trustworthy, not merely empty.
- `failure_when` is now tri-state (fixes #34). A declared value predicate distinguishes MATCH
  (declared failure), NO_MATCH (field present, not a failure value — a clean pass), and UNKNOWN (the
  pointed-to field is absent, so the predicate cannot be evaluated). On a side-effecting tool an
  UNKNOWN result is disclosed as a suppression ("cannot verify it did not fail"), never a silent
  clean pass — so an API that drops the field no longer sails through the contract written to catch
  its failure. A new `"optional": true` predicate key opts a legitimately-absent field back into a
  clean pass. `FailurePredicate.matches()` is unchanged (MATCH-only), so existing rules are not
  affected.
- LangSmith adapter: `from_langsmith_run` and `--format langsmith` normalize nested LangSmith run
  trees into canonical traces, preserving structured tool errors for R2. Robustness fixes:
  integer `execution_order` now sorts numerically (not lexically), positional-only tool args are
  preserved instead of dropped to `{}`, and a run-level numeric HTTP status is read as an error.
- Fault-injection experiment harness (`run_experiment` / `render_experiment`): runs an agent at
  baseline and under injected faults, `runs` times each, and reports recovery rate,
  incorrect-continuation rate (the agent claimed success while the oracle failed), and
  tracelint-flagged rate — each with a Wilson interval. Unlike the scripted `scorecard --demo`, it's
  built to run a *real* agent (see `examples/fault_experiment.py`) so the numbers are observed, not
  authored.
- New `DENIED` fault type: a transport success (HTTP 200, status OK) carrying a `{"status":
  "declined"}` body — invisible to structured-error detection, flagged only when the tool declares a
  `failure_when` predicate. The experiment prints the before/after across that declaration.
- Adapter conformance suite (`tests/conformance/`): per-adapter fixtures that pin *normalization*
  only — raw provider payload → exact canonical steps, no rules — for OpenAI (standard chat +
  ShareGPT), OTel/OpenInference (Phoenix top-level `span_kind` + GenAI `execute_tool` semconv),
  Langfuse, and LangSmith. A regression guard against the trace-*misreading* bugs that are worse
  than a missing rule.

## [0.5.0]

- Fix (found by linting real Phoenix agent traces): the OpenInference adapter no longer crashes on
  `get_spans_dataframe()` records whose `events` cell is a numpy array (`array or []` raised "truth
  value is ambiguous").
- Fix (same): tool arguments serialized as a Python `str(dict)`/`repr` (single-quoted keys, common
  in real instrumentation) are now parsed via `literal_eval` instead of being reported as **malformed
  arguments (R6, a hard_defect)**. This was a false CI-failing finding on every such call — and the
  unparsed empty args also faked identical calls, producing false R4 loops. Both classes are gone.
- The OpenTelemetry event-list reader now understands **both** OTel conventions: OpenInference
  *and* the OTel **GenAI** semantic convention (OpenLLMetry / Traceloop). A GenAI span is read via
  `gen_ai.operation.name` (`execute_tool` → tool call, `chat` → LLM), with `gen_ai.tool.name`,
  plain `input`/`output`, and provenance seeded from `gen_ai.input.messages`. One reader now covers
  most observability-platform exports, not just Arize/OpenInference.
- The message-list reader (`from_openai_messages` / `--format openai`) now reads the common
  real-world variants without a bespoke adapter: the **ShareGPT** `from`/`value` shape (with
  `human`/`gpt` roles), a `role`+`text` shape (some trajectory dumps), and **typed-block content**
  (`[{"type":"text","text":...}]`, the Anthropic / newer-OpenAI / SWE-bench form). Typed blocks are
  flattened to text for messages and text tool-results, while a *structured* tool-result payload (a
  dict or data list) is preserved so R2 / `failure_when` can still read it.

## [0.4.2]

- `metadata.failure_when` gains `contains` (substring) and `matches` (regex) modes, and `pointer`
  may be `""` (the whole result) — so a tool that reports failure as **free text** (the common MCP
  `"Error: ..."` string over a 200) can declare that contract structurally, not just tools with a
  `/status` field. The declaration lives in the operator's `tools.json`, so it works for
  third-party tools whose authors declare nothing.
- R2a's exception-text heuristic now also matches `failed` / `failure` (not just `error` /
  `exception` / tracebacks / HTTP 4xx-5xx), still at the candidate tier. Both raised in review.

## [0.4.1]

- CI on-ramp: a composite **GitHub Action** (`uses: AshwinUgale/tracelint@v0.4.1`) and a
  **pre-commit hook** (`.pre-commit-hooks.yaml`), plus an "Add to CI" guide in the README, so a
  build can gate on `tracelint check` in a few lines. No library changes.

## [0.4.0]

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
