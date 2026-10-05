# Changelog

All notable changes to tracelint are documented here. This project adheres to
[Semantic Versioning](https://semver.org) (pre-1.0: minor versions may introduce
additive features; the public API is not yet frozen).

## [Unreleased]

- **New: DeepEval metric (`pip install "tracelint[deepeval]"`).** Run tracelint's deterministic
  checks as a [DeepEval](https://deepeval.com) metric, so an existing DeepEval suite gets a
  judge-free structural check beside its other metrics. `TracelintMetric` passes a test case iff the
  run's trace passes the rules, with the same gate as `tracelint check` (`fail_on`). The trace is
  bound to the metric (`trace=`) or carried on the test case's `additional_metadata`; a missing or
  unreadable trace fails that case with a recorded reason instead of crashing the run. The scoring
  core (`score_trace`) is dependency-free and the SDK is imported lazily, so importing
  `tracelint.integrations.deepeval` never requires DeepEval. First of the §3.6 distribution
  wrappers. See [docs/integrations/deepeval.md](docs/integrations/deepeval.md).

- **SARIF results anchor to the finding's trace step.** A result's `region.startLine` was a constant
  line 1; it is now the line of the trace file where the finding's first step can be located by its
  source token (an OTel/Phoenix `span_id`, a Langfuse `observation_id`, or a `call_id`), so a GitHub
  code-scanning annotation lands on the offending record instead of the top of the file. Best-effort
  and fail-safe: a step that can't be located unambiguously (a span merged in from another file, a
  repeated `call_id` in a multi-trace `.jsonl`) stays at line 1, and the exact `step_indices` remain
  in the result's `properties`. Completes the §3.7 report-readability work.

- **Per-rule help pages.** Every rule a finding names now has an entry in the new
  [`docs/rules.md`](docs/rules.md) reference — what it checks, its tier, and the honest moves to
  resolve it — each under a stable anchor. The SARIF `helpUri` now links a GitHub code-scanning
  alert straight to its rule's anchor (previously every rule pointed at the repository root), and
  the text report ends with a pointer to the page whenever it names a rule. A test keeps the page,
  the shipped rule set, and the help links from drifting apart. (The remaining report-readability
  item — mapping a SARIF result's `startLine` to the finding's trace step — is still open.)

- **OTel-GenAI ingestion: read `gen_ai.tool.definitions`.** The OTel adapter already read tool
  schemas from a TOOL span's `tool.parameters` and from OpenInference `llm.tools.*.json_schema`;
  it now also reads the OTel GenAI semantic-convention `gen_ai.tool.definitions` list (each
  `{name, parameters}` / `inputSchema`), whether the attribute lands as a JSON string or a list.
  Those schemas feed each call's schema, powering `tracelint init` and R11 (contract drift), so
  a GenAI-native instrumentation lights up R1 / R3 / R11 from the trace alone. (MCP tool calls
  already arrive as `execute_tool` TOOL spans, and `invoke_agent` is already handled — closing
  the ingestion half of §3.5.)

- **New: `tracelint doctor`.** A trace where rules abstain used to show a wall of suppressions; `doctor` turns that into the one sentence that matters. It lints the trace(s) and reports *why* each rule couldn't run, grouped by cause: **tool content not recorded** (your instrumentation isn't capturing tool arguments/results — it names the capture setting for the format), a **missing `tools.json`** (run `tracelint init`), or simply **not applicable** (no gap). It adds a coverage summary (evaluatable / total per rule, across all traces) and is advisory — it never gates (always exits 0). `tracelint doctor <traces> [--format F] [--tools T]`.

- **New: R12, unresolved side effect.** The side-effect-integrity arc's last core rule, and the
  case R2 misses: not "the agent ignored an error" but "the agent never found out." R12 flags a
  side-effecting tool whose last call has no recorded outcome, or failed with nothing after it
  succeeding. It is a `candidate`, fail-closed (the ROADMAP's explicit tier call): a missing
  result could be a truncated capture as easily as a real hang, so R12 discloses a signal for
  review and never asserts a defect — and marks it a possible false positive when the call sits
  at the very end of the trace, dropping that caution when the run clearly continued past it.
  Opt-in: only side-effecting tools, judged per tool on its last call, so a later success
  recovers an earlier failure. Independent read-back confirmation (a declared `confirmed_by`) is
  the deeper, deferred half.

- **New: R11, contract drift.** Some traces carry each tool's run-time schema inline (OTel /
  OpenInference tool definitions, an OpenAI `tools` block, on `ToolCall.schema`). R11 compares
  that against the committed `tools.json` structurally — property names, each property's `type`,
  `required`, the top-level `type`, ignoring descriptions/titles/order — and flags a real
  difference as *contract drift*: the committed contract has gone stale, so R1 is validating
  against a schema that no longer matches the tool. Like R10 it is a `hard_event` (certain, but
  about the contract rather than the agent's run) — shown but not failing CI unless you opt in
  with `--fail-on hard_event`. Opt-in and quiet: it fires only where a tool carries both an
  inline and a committed schema and both declare properties, so native traces and bare
  placeholders stay silent. This completes the MCP-native-contracts group (`init --from-mcp`,
  R10, R11).

- **New: R10, the result contract.** A tool can declare an `output_schema` (MCP's `outputSchema`
  maps straight to it, and `tracelint init --from-mcp` now captures it). R10 validates each
  recorded successful result against it — the mirror of R1 on the arguments. A violation is
  *tool output drift* and a `hard_event`: the output is the tool's, not the agent's own defect
  (unlike R1's `hard_defect`), so it is shown but does not fail CI unless you opt in with
  `--fail-on hard_event`. Opt-in: a tool with no `output_schema` is silent (no finding,
  suppression or coverage). Error results are skipped (R2's domain); a success whose content
  wasn't recorded is disclosed as not-checked.

- **New: `tracelint init --from-mcp`.** Point it at a saved MCP `tools/list` response and it
  writes a starter `tools.json`: `inputSchema` becomes each tool's argument schema, and the
  server's `readOnlyHint` / `idempotentHint` annotations become `side_effecting` / `idempotent`.
  Because those annotations are advisory (the MCP spec says clients must not rely on them) and
  `side_effecting` drives the hard-defect rules, they are applied only when the server states
  them explicitly — a tool with no hint is left with a `_todo` to classify, not a guessed
  default, so a server can't manufacture a false red. Accepts the JSON-RPC envelope, a bare
  `{"tools": ...}`, or a plain list. (First of the audit's MCP-native-contracts items; a
  result-contract rule from `outputSchema` and a contract-drift check follow.)

- **Call/result pairing is no longer O(n^2) on long traces.** `Trace.call_for`, `result_for`
  and `pairs` scanned the step list on every lookup, so the rules that pair calls with results
  — R2a, R2b, R4, R5 — grew quadratically with trace length. A trace now builds a `call_id`
  index once and looks up by bisect, bringing those rules back to roughly linear: on an
  8,000-call trace R2a drops from ~0.6s to ~0.04s and R2b/R4/R5 from ~0.4s each to ~0.1s, with
  identical findings. (R3's provenance substring scan is a separate, narrower cost — it only
  bites a trace that is both very long and hallucination-heavy, since a derivable value
  short-circuits — and is left for a dedicated change.)
- **The pytest fixture and `tracelint langfuse check` now honour the project config.** Both read
  `[tool.tracelint]` / `tracelint.toml` for rules, tools, `fail_on` and ignores, so a `trace_capture`
  test or a checked Langfuse trace gates exactly as `tracelint check` does in CI — no silent drift
  between what CI enforces and what a test or a `langfuse check` accepts. (A baseline is not applied in
  either: it accepts findings in committed trace files, so pair `tracelint langfuse pull` with
  `tracelint check --baseline` for that.)
- **Suppressions now say which fix they need.** A rule that abstains was always disclosed, but
  every suppression read the same and the report always suggested `tracelint init` — even when
  `init` could not help. Each suppression now carries a category and the report groups them:
  *missing a tools.json fact* (run `init`), *not recorded in the trace* (enable tool-content
  capture), and *not applicable* (too few calls, no results — nothing to check). The closing
  line suggests `init` only when a contract is actually missing, and the category is in the JSON
  as `suppressed_category`.
- **Clearer reports (output readability).** The text report now leads with the trace **file path**
  instead of only the run id (keyless native traces no longer print a bare `: 0 finding(s)`), a run
  over several files gets a one-line cross-file **summary** header, and a short **tier legend**
  explains the `[hard_defect]` / `[hard_event]` / `[candidate]` labels.
- **JSON output no longer folds suppressions or ignored findings into `findings`.** `findings` is
  now the active findings only, so its count matches the text report; suppressions (not checked) and
  ignored (accepted) findings are disclosed in their own `suppressions` and `ignored` arrays, and
  each report carries its `source` file path.
- **New: R9, declared preconditions.** R2b proves an agent acted on a failed result only when a value
  flows from the failure into the action, so the audit's deploy after an `UNSTABLE` pipeline was a
  hard defect when the agent copied the build id but exited 0 when it deployed `"latest"` or a
  version number, and a refund after a failed lookup of an id the user gave is only an R2b candidate.
  A tool can now declare what must succeed before it runs:
  `"requires": [{"tool": "get_order", "same": ["order_id"]}]`.
  - The **latest** returned call of the required tool decides: a retry that passes satisfies it, a
    later failure un-satisfies it, and a call still in flight (fired in parallel, not waited for)
    doesn't count. `same` scopes it to one entity (refunding order B needs B's lookup); `"succeeded":
    false` asks only that the call returned.
  - A violation is a `hard_defect`, whether the required call failed or never ran. Success is read
    like R2a reads it; when the trace can't show it (no error, no `failure_when` to read), or the
    arguments that scope it weren't recorded, the check is disclosed as not run, with the fix named.
  - Opt-in: a trace whose tools declare no `requires` gets nothing new in its report. A malformed
    `requires` entry is an input error (exit 3). The contract view lists it, and `tracelint init`
    suggests it in the `_todo` of a tool that was called after others.
  - Checks are indexed per trace: 8,000 calls with a precondition each take ~0.04 s.

- **New: a CI baseline with a coverage ratchet** (the second half of the CI contract). Adopting
  tracelint on traces that already had findings meant a red build until every one was fixed, or
  turning rules off.
  - `--update-baseline` records what the traces show (`--baseline FILE`, or `baseline` in the
    config); later runs accept up to that many of each finding and fail on anything beyond it.
    A finding is matched by its trace file and its rule, kind, tools, fields and signal, never by
    step position, value or run id, so a re-run of the agent whose steps shift still matches. A
    recorded finding also accepts the same finding at a lower tier, never a higher one.
  - Only findings at the gate (`fail_on`, a hard defect by default) are recorded. Accepted ones
    stay in the report as ignored, "in the baseline"; ones that no longer occur are noted.
  - **Coverage ratchet:** the baseline records which tools each rule could not check and which
    rules checked something. A run fails (exit 1) when a rule can no longer check a tool it could,
    or a rule that checked something now checks nothing: content capture switched off, schemas
    deleted. `ratchet = false` or `--no-ratchet` turns it off; a trace file the baseline never saw
    has nothing to compare.
  - Paths are stored relative to the baseline file, and updating from some traces keeps the
    entries for the others. A missing or invalid baseline is an input error (exit 3).
  - R2b's evidence names every side-effecting tool the failure reached (`side_effecting_uses`), so
    a baselined failure that starts feeding a new action is a new finding.
- **New: a project config file, `--fail-on`, and ignores with reasons** (the first half of the CI
  contract). Every setting had to be repeated on each CI command, nothing below a hard defect could
  fail a build, and the only way to accept a known finding was to turn its rule off.
  - `[tool.tracelint]` in `pyproject.toml`, or a `tracelint.toml`, sets `format`, `tools` (relative
    to the file), `rules` and `fail_on`. The nearest file wins, searched up to the repository root;
    `--config FILE` names one; flags override it. The GitHub Action's `format` input now defaults to
    the config's.
  - `fail_on = "hard_event"` (or `--fail-on hard_event`) also fails the run on hard events, and
    `"candidate"` on candidates too, with **exit 1**; a hard defect still exits 2. A candidate that
    fails the run is always shown in the text report.
  - `[[tool.tracelint.ignore]]` accepts one rule's findings, optionally narrowed to a `tool`, an
    argument `field` or a trace `path` glob, and requires a `reason`. An ignored finding stays in
    the report (text: an "ignored" section with the reason; JSON: `ignored_reason`) but no longer
    counts toward the exit code. An ignore that matches nothing prints a warning.
  - A misspelled key, an unknown rule or tier, an ignore without a reason, or invalid TOML is an
    input error (exit 3), so a typo can't quietly loosen the gate.
  - Python 3.10 reads the config with `tomli` (added as a dependency there); 3.11+ uses `tomllib`.

## [0.10.0]

A correctness release. An audit ran 0.9.0 on real framework traces and through every documented CI
path, and found false passes (input linted as nothing, failures read as success) and false failures
(missing data treated as evidence, value overlap read as dataflow). This release fixes them, so
results change on existing traces; the change you may need to act on is marked **Behavior change**
under R2b.

- **The GitHub Action installs the version you pin.** `uses: AshwinUgale/tracelint@v0.10.0`
  installed the latest tracelint on PyPI whatever the tag, so a pinned workflow changed when a
  release came out. It now installs the code at the pinned ref; `version:` still installs a named
  PyPI release. CI now runs the action itself.
- Docs: the README rule table lists R8 and R6's candidate tier; the library example reads any format with `load_source`
  (`Trace.load` reads only native traces, so it failed on a captured one); the README's links work
  on PyPI; the Action and pre-commit examples pin v0.10.0 (pre-commit still said v0.4.1); the
  Langfuse guides lead with `tracelint langfuse pull`; CONTRIBUTING describes the current layout and
  what CI checks; `--rules` help shows real rule ids (`R2a`, not `R2`); the roadmap lists declared
  preconditions.
- Packaging: a `py.typed` marker; classifiers for Python 3.13 and 3.14 (CI now runs 3.10 to 3.14);
  the license as an SPDX expression (`MIT`) instead of the whole license text; tracelint.com as the
  homepage.
- **Fix: R3 reads a value's digits from one number, compares numbers by value, and no longer
  slows down with the square of the trace.**
  - **Missed defects.** R3 joined every digit in a text, so a fabricated `ORD-58213` "derived" from
    a result with a total of 58 and a quantity of 213, even on a field annotated `provided`; two
    observed numbers could also be run together (58 and 213 as 58213). A value's digits must now
    come from one number in the text (a phone number written with separators still counts), and
    two numbers are never joined without a separator.
  - **False failures.** The same number written differently was underivable: `1200.0` against
    `$1,200`, a float `4906.0` against `4906`, `12` in "order 12 items". On a `provided` field that
    was a hard defect on a correct call. Numbers now compare by value.
  - **Cost.** R3 rebuilt the provenance graph for every call and compared every pair of observed
    values for each argument. One 1,000-row tool result with two free-text arguments took about a
    minute, and so did a run of 1,000 small calls. One graph is now grown through the trace, with
    its values indexed: both take a fraction of a second. `ProvenanceGraph.observe(step)` adds a
    step to a graph.
- **Fix: R2b follows the data, so handling an error no longer fails CI.** R2b read any later call
  that shared a value with a failed result as using it, and a failed result often echoes its inputs.
  On real LangGraph runs, retrying a timed-out cancellation with the same id, and emailing a receipt
  alongside a fraud check that failed, were hard defects (exit 2); so was charging the backup card
  the user named after a decline. Now a value counts only when nothing else the agent observed
  supplied it, give or take case and separators (`A-100` is `A100`), and retries and recoveries of
  the failed tool are skipped. Another failed result is not a source (a retry that fails again
  repeats the value), and neither is a call the value was passed to.
  - **Missed defects.** Only the first call sharing a value was examined, so a logging call hid
    the side-effecting transfer after it (a candidate); it is now a hard defect. Each misuse is
    reported once, against the first failure that supplied it.
  - **Behavior change.** A side-effecting call after a failed lookup that uses only an id the user
    gave (`get_order(A100)` fails, then `refund_order(A100)`) is no longer a hard defect: the refund
    used none of the failure's data, and R2b reports the failure as not retried (a candidate).
    Whether a refund needed that lookup to succeed is a precondition, not dataflow; declared
    preconditions are planned. When a call the value was passed to returned it (a lookup that may
    have confirmed it, or a log that echoed it), the use is a candidate naming that call.
  - The Phoenix, Langfuse and Traceloop examples and notebooks planted the old shape. Their failed
    `get_order` now returns a cached payment method that the refund uses, so they still exit 2, for
    a reason R2b can prove.
- **Fix: one rule for reading a tool's result, in every adapter** (the result half of #37). Each
  adapter unwrapped results, and decided what counts as an error, on its own, and they had drifted:
  - **Missed defects.** A declined charge followed by shipping failed CI from OTel and an OpenAI
    dict, but passed from an OpenAI JSON-string result, Langfuse, and LangSmith (including a real
    LangSmith trace): the `failure_when` pointer never reached the result. LangChain's
    `ToolMessage` (nested in OpenInference, flat in Langfuse and LangSmith, inside LangSmith's
    `{"output": ...}`) is now unwrapped everywhere, and a JSON-string result is parsed.
  - **False failures.** `"error": false` or `""` counted as an error, and so did a status code in
    the result's body (a link checker's `status_code: 404`), failing CI on correct runs. Now an
    error is the span's or run's own error status, a `ToolMessage` with `status: "error"`, or a
    non-empty `error` field in the result. A `status`, `http_status` or `status_code` inside the
    result is the tool's data: R2a shows a failure-looking value as a candidate that names
    `failure_when`, which makes it a fact. LangSmith and Langfuse no longer read a `status` inside
    the result as structured (OTel and OpenAI never did); LangSmith's run-level `status` still is.
  - **Crash.** An `http_status` recorded as a string (`"404"`) raised a `TypeError` and exited 3;
    it is read as a number.
- **Fix: `capture` no longer breaks your own tracing, and never passes on an empty capture.** If the
  framework was already instrumented (a Phoenix or Langfuse setup, as smolagents' telemetry docs
  show), capture recorded **0 spans**, and its `uninstrument()` on exit removed the shared
  instrumentor, so **your own backend got nothing from every later run**. The 0.8.0 note said
  existing tracing was left untouched; it was not. Now capture never uninstruments what it did not
  instrument, and records from the global tracer provider (which `phoenix.otel.register()` sets).
  Verified on the real smolagents and LangChain instrumentors: with a global provider the run is
  captured and your tracing keeps working; with a provider capture can't reach it raises and names
  the fix. A capture that records nothing raises instead of writing `[]`, which linted as a clean
  run; an exception from the agent itself still propagates unchanged.
- **Fix: the `trace_capture` fixture lints every run in the block** (`.reports`, one per run;
  `.report` still works for one run). It used to fail with "use `lint_otel_traces`", a call only
  the fixture could make. A capture with nothing to lint now fails the test instead of passing.
- **Fix: usage errors exit 3, not 2.** argparse's own code, 2, is the one tracelint reserves for a
  hard defect, so a mistyped flag in a CI step read as "defect found".
- **Fix: no crash printing text the console can't encode.** Redirected output on Windows uses the
  locale code page, and a report echoes the trace's own text (CJK, emoji): a clean run exited 3
  with a `'charmap' codec` error. Such characters are now escaped (`\u6771`).
- `pip install "tracelint[capture]"` is now named when the OpenTelemetry SDK is missing, instead of
  a raw `ModuleNotFoundError`. The integration guides' snippets read traces as UTF-8.
- **Fix: the wrong `--format`, or an empty file, is an input error instead of a clean pass.**
  The CLI's default format (and the GitHub Action's) is `native`. Read as native, a span file
  became one empty "trace" per span and exited 0 having checked nothing; the same happened for
  every other format mismatch, and for an empty file (`[]`, an empty `.jsonl`). Across 12 test
  inputs (a trace in each format, empty files, a CHAIN-only export) and the five formats, all 60
  combinations exited 0 on 0.9, though only the 7 matching pairs linted anything. Now a
  file in which nothing reads as the requested format exits 3, naming the format it looks like
  (`it looks like OpenInference / OTel spans: use --format openinference`). Native input must be
  native traces (objects with a `steps` list; a bad `.jsonl` line is named), and a right-format
  file with no tool calls or messages is reported as "nothing to lint". `load_source` raises the
  same `ValueError`; `check` checks each file after merging runs split across files.
- **Fix: `.jsonl` span files are linted as runs, not as one trace per line.** Exporters write one
  span (or one export batch) per line, as Phoenix's `to_json(..., lines=True)` and OTel file
  exporters do. Every CI path in the docs (the GitHub Action example, the pre-commit hook, the
  plain `tracelint check traces/*.jsonl` command) linted each line as its own trace, so a defect
  spanning steps could never be seen. On a real Phoenix JSONL export of 10 failed-release runs,
  0.9 reported 110 one-span "runs", exited 0, and missed all 10 defects. With `--format
  openinference`/`otel`, a `.jsonl` file is now read as one span collection and regrouped by
  trace id. `check` also merges a run whose spans are split across files (a rotating exporter, a
  glob over batches) and reports it under the first file; a span seen twice is kept once. Lines
  that each hold a whole run and carry no trace ids still lint one run per line.
- **Fix: OpenAI `.jsonl` and ShareGPT datasets.** A `.jsonl` file of single messages is one
  conversation, as the docs said; 0.9 made each message its own trace, so tool results never met
  their calls. ShareGPT datasets (`{"conversations": [...]}`, `from`/`value` messages) loaded zero
  traces and exited 0 with no output; they now lint one conversation each.
- **Fix: lossy argument records no longer fail CI — the arguments are *unknown*, not `{}`.**
  Instrumentation often records a tool call's arguments lossily: as a bare value (LangChain
  single-input and LCEL tools), redacted (`OPENINFERENCE_HIDE_INPUTS`), positionally with no
  parameter names (`{"args": ["A100"], "kwargs": {}}` from smolagents, Langfuse `@observe`,
  LangSmith), or not at all (OTel GenAI without content capture). The adapters turned these into
  `{}` or a made-up object, so R1 reported every required field missing, R6 called the record
  malformed JSON, and R8 compared made-up values: **exit 2 on valid runs**. Where the trace has the
  real arguments (the `tool_call` the model emitted), they are recovered; otherwise the call carries
  `ToolCall.args_unavailable` with the reason. R1 suppresses such a call with that reason, and R2b,
  R3, R4, R5 and R8 disclose the calls they could not check, so an unknown never reads as a clean
  pass.
- **Changed: R6 is a hard defect only for text the model emitted.** Broken JSON in a tool's own
  record, with no model call in the trace to confirm it, is now an R6 `candidate`: an exporter
  truncating a long input (a coding agent's `write_file` content, say) leaves the same text, and
  that failed CI on every such run.
- **Fix: one argument normalizer for every adapter** (`adapters/_common.py`, the argument part of
  #37). OpenInference/OTel, Langfuse and LangSmith now read the same record the same way. A
  positional argument object keeps its names, and a real parameter named `args`
  (`run_command(command, args)`) is no longer mistaken for a call envelope.
- **Fix: a false R8 duplicate from mispaired calls.** LangChain records some TOOL inputs as bare
  values and others as objects, and recovery paired a later bare span with an earlier call's
  arguments — a "duplicate SMS" that never happened. Each TOOL span now claims its own model
  `tool_call`: by id when the trace records one, else in order. Model calls with empty or malformed
  arguments are kept in the pairing instead of dropped.
- **Fix: OTel GenAI tool content.** `execute_tool` spans' `gen_ai.tool.call.arguments` and
  `gen_ai.tool.call.result` are read; when content capture is off, the reason names the opt-in
  (`OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT`).
- Native JSON now round-trips a call's `args_unavailable` and discovered `schema`.
- Regression fixtures: real lossy records in `tests/fixtures/lossy_args/` (LCEL string tool,
  LangGraph with hidden inputs and outputs, hidden inputs only, optional arguments, a LangSmith LCEL
  run), generated offline with a scripted model.

## [0.9.0]

Found by running a real LangGraph 1.2 agent (openinference-instrumentation-langchain 0.1.76,
gpt-4o-mini) through a local Arize Phoenix and linting the result — a release agent that deployed a
Jenkins `UNSTABLE` build to production. Both real traces are now regression fixtures
(`examples/traces/langgraph_phoenix_trace.json`, `langgraph_capture_trace.json`).

- **Fix: a false CI failure on real Phoenix exports.** Phoenix's span dataframe returns message
  attributes (`llm.input_messages`, `llm.output_messages`, `llm.tools`) *unflattened* into lists of
  relative-keyed objects; the OTel adapter only read the flat dotted form. It lost the user's request
  (a false R3 candidate) and, on LangChain's lossy single-argument TOOL inputs, could not recover the
  real arguments from the LLM span — a false **R6 hard defect, exit 2, on a valid call**. The adapter
  now rebuilds the flat OpenInference keys from nested attributes (also handles numpy arrays from an
  in-memory dataframe).
- **Fix: "acted on a failed result" was missed for LangChain / LangGraph.** Their instrumentation
  records a tool's output as a serialized `ToolMessage` (`{"type": "tool", "data": {"content": ...}}`),
  so a `failure_when` pointer never matched and R2a/R2b could not see the failure. The envelope is now
  unwrapped to the tool's real result, and a `ToolMessage` `status: "error"` counts as a structured
  tool error.
- **Fix: a multi-run Phoenix export was linted as one merged trace.** Trace-id grouping now reads the
  dataframe's flat `context.trace_id` column, so `tracelint check` splits it into one report per run.
- **New: `lint_otel_traces(spans)`** — one report per trace, the shape a Phoenix project export
  actually has. `lint_otel_trace` / `from_otel_spans` now **raise** on spans from several traces
  instead of silently merging them (a merged "trace" manufactures false loops and redundant calls).
- Docs: the Phoenix, Langflow, and README snippets use `phoenix.client.Client().spans
  .get_spans_dataframe(...)` with `lint_otel_traces`.

## [0.8.0]

- **`tracelint.capture` — record an agent run to a lintable trace.** The primary path assumed you
  already had a trace file; now `with capture("trace.json", framework="smolagents"): agent.run(...)`
  captures one by wrapping the framework's stock OpenInference instrumentor against a *local* OTel
  provider whose only exporter writes the flat OpenInference span shape — so the file lints with
  `--format openinference` exactly like a hand-exported one, and any tracing you already run is left
  untouched. Covers the in-process frameworks (smolagents, LangGraph / LangChain, CrewAI) via opt-in
  extras `tracelint[capture]` / `[capture-smolagents]` / `[capture-langchain]` / `[capture-crewai]`;
  Langflow uses an OTel-to-file recipe. The OTel SDK and instrumentors are imported lazily, so
  importing tracelint never requires them.
- **`tracelint langfuse pull <trace-id> [-o file]`.** Fetch a Langfuse trace straight to a file (as
  native tracelint JSON) so `pull` → `check` composes and the file doubles as a saved fixture.
  Read-only; reuses the existing `LANGFUSE_*` auth and turns a vendor/network/auth failure into a
  clean exit `3` that names the env vars to check but never echoes their values. Default output is
  `<trace-id>.json`.
- **Write-back demoted.** `pull` is now the featured Langfuse verb; `langfuse check --write-back` is
  repositioned as an advanced recipe — labeled "(advanced)" in `--help` and moved out of the primary
  README path. It still works: repositioned, not removed.
- **`trace_capture` pytest fixture.** Opt in with `pytest_plugins = ["tracelint.pytest_plugin"]` in
  `conftest.py`, then a test captures an agent run and lints it inline — a hard defect fails the test
  (candidates never do); `assert_clean=False` exposes the report on the handle's `.report` to assert
  on yourself. A thin wrapper over `capture` + `lint_otel_trace`; no new dependency.
- **Dev-tool / CI-first repositioning.** README and VISION now lead with the capture → lint → CI core
  loop and the three-tier platform model (core depends on no platform; read convenience; write-back
  demoted and frozen). OpenTelemetry / OpenInference is presented as a *format*, not a platform.
  Per-framework "capture a trace in a test" recipes were added to the integration one-pagers, and
  `docs/DIRECTION.md` records the change.

## [0.7.0]

- **First-run hardening.** A malformed or unfamiliar trace shape now degrades to a clear error and
  exit `3`, never a Python traceback: the native loader validates that `steps` is a list, each step
  is an object, and a `tool_call`'s `args` is an object (with a specific message for each), and the
  CLI has a last-resort handler that turns any unforeseen error into a "please report it" message
  pointing at the issue tracker. A clean run with suppressed rules now prints an explicit "no
  structural issues found" line (and points to `tracelint init`) instead of only a bare count.
- **`tracelint init` — schema inference + inline TODOs.** When the trace declares no schema for a
  tool, `init` now **infers** an object schema from the argument values actually observed (dropping a
  type when it varies across calls; never guessing `required`), marked with a `$comment`. Each tool
  entry carries a `_todo` list naming exactly what to fill in (schema review + `side_effecting` /
  `idempotent` / `failure_when`), and the file has a top-level `_comment`; both are ignored on load,
  so the draft still round-trips through `--tools`.

## [0.6.0]

- **`tracelint init` — bootstrap a `tools.json` from a trace.** The #1 onboarding step was writing a
  contract by hand; now `tracelint init spans.json --format openinference -o tools.json` reads a
  trace, discovers the tools called, and fills in each tool's argument **schema** from the telemetry
  (OpenInference `tool.parameters` / `llm.tools.*.tool.json_schema`), leaving the **behavior** it
  can't infer (`side_effecting` / `idempotent` / `failure_when`) as explicit `null` placeholders and
  printing the review TODOs. The result is a valid contract that round-trips through `--tools`.
  Framework-internal control tools (e.g. `final_answer`) are skipped, matching R7. `ToolCall` gained
  an optional `schema` field (discovery-only — the rules still validate against the operator's
  `tools.json`, not this), populated by the OTel/OpenInference adapter.

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
