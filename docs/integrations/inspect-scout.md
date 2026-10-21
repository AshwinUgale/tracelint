# tracelint + Inspect Scout

[Inspect Scout](https://meridianlabs-ai.github.io/inspect_scout) scans agent transcripts with
*scanners*: functions that read one transcript and return results citing the messages they are
about. It reads Inspect eval logs, and imports traces from Arize Phoenix, LangSmith, Logfire, W&B
Weave, MLflow, Claude Code and Harbor (ATIF). tracelint ships its rules as Scout scanners, so a
Scout user gets reference-free trajectory checks with **no model call**: tool errors, loops,
redundant and malformed calls, and with a contract the schema, side-effect and integrity rules. They
run beside the scanners you already use, and every finding links to its message in `scout view`.

```bash
pip install "tracelint[scout]"      # Inspect Scout needs Python 3.11+
```

## Scan

The scanners register under the `tracelint` namespace (through the `inspect_ai` entry point), so the
Scout CLI finds them by name. No `--model` is needed:

```bash
scout scan tracelint/tracelint -T ./logs                            # the keyless rules
scout scan tracelint/tracelint -T ./logs -S tools=tools.json        # + the contract rules
scout scan tracelint/tracelint -T ./logs -S rules=R4,R5 -S min_tier=hard_event
scout scan tracelint/tool_loops -T ./logs                           # one rule
```

Traces from another platform are imported first, then scanned the same way:

```bash
scout import claude_code -T ./transcripts                           # ~/.claude/projects
scout import atif -T ./transcripts -P path=jobs/                    # Harbor (pip install harbor)
scout import phoenix -T ./transcripts -P project=my-agent
scout scan tracelint/tracelint -T ./transcripts
```

From Python:

```python
from inspect_scout import scan, scan_results_df, transcripts_from
from tracelint.integrations.scout.scanners import tool_loops, tracelint

status = scan([tracelint(tools="tools.json"), tool_loops()], transcripts=transcripts_from("./logs"))
rules = scan_results_df(status.location).scanners["tracelint"]
print(rules[["transcript_id", "label", "value"]])
```

## Scanners

| Scanner | Rules | Result |
|---|---|---|
| `tracelint` | R2a, R4, R5, R6; with `tools`, also R1, R7, R8, R9, R10, R12 and R13 (only if the contract declares `off_limits`) | one result per rule, labelled with its finding type |
| `tool_errors` | R2a: a tool returned an error | one result |
| `tool_loops` | R4: the same call repeated with no progress | one result |
| `redundant_calls` | R5: a call repeating an identical earlier call and result | one result |
| `malformed_arguments` | R6: arguments the model emitted as invalid JSON | one result |
| `record_quality` | not a rule: is the record complete? ([below](#record-quality-judging-the-record-not-the-agent)) | one result; `value` is a list of issue codes |

The rule scanners take:

- `tools`: a `tools.json` contract, as a path or its dict. `tracelint init` drafts one from a trace,
  and `tracelint init --from-mcp` from an MCP server's tool list.
- `min_tier`: the lowest tier counted. `candidate` (the default) counts everything, `hard_event`
  drops heuristics, `hard_defect` keeps only provable defects.

`tracelint` also takes `rules`, the rule ids to run (`-S rules=R4,R13`, or a list in Python). Any
rule can be named, including R2b, R3 and R11. Each rule is described in the
[rule reference](../rules.md).

## Rule results

Each rule scanner's result follows Scout's `grep_scanner`:

- `value` is the number of findings at or above `min_tier`. It is `0` when there were none, and also
  when the rule has nothing to look at (R4 on a run with two calls). It is **`None` when the rule
  could not check the transcript**: the contract lacks what it needs (no schema for any called
  tool, no `off_limits`), or the transcript did not record it. A rule that could not run is never
  reported as a clean zero.
- `explanation` lists each finding with its tier and the messages it cites, for example
  `[M5] [M7] [M9] (candidate) 'search' called 3 times in a row with identical arguments and an
  identical result`. Message numbers count from 1 over the transcript's messages, as Scout's own
  scanners do. It ends with anything not checked (`Not checked: no schema for tool 'web_search'`).
- `references` link each cited message, so `scout view` jumps to it.
- `metadata` has the same keys on every result: `rule` (`R4`), `tiers` (the count per tier),
  `findings` (each finding's full evidence, plus the messages it cites), `coverage` (how many tool
  calls or results the rule could check), and `not_checked`.
- `label` is the rule's finding type (`tool_error_event`, `loop`, `redundant_call`,
  `malformed_arguments`, `schema_violation`, `off_limits_source`, …) on the `tracelint` scanner,
  and empty on the single-rule ones.

`scan_results_df` gives the `tracelint` scanner one row per transcript and rule. It spreads the
`metadata` of these rows into columns: `metadata.rule`, `metadata.tiers`, `metadata.findings`, and
so on. The single-rule scanners keep `metadata` as one JSON column.

## Record quality: judging the record, not the agent

The rule scanners judge what the agent did. `record_quality` asks something earlier: does the
transcript record the run completely enough to trust those verdicts, or a judge's? It runs the
[record-quality checks](../record-quality.md) behind `tracelint doctor`, one result per
transcript, and never counts anything against the agent.

```bash
scout scan tracelint/record_quality -T ./transcripts
```

- `value` is the list of the record's issues, as codes. It is `[]` when there are none, the same
  shape as a Scout multi-label answer. `scan_results_df` keeps the list as JSON text, so read it
  with `json.loads`.

  | Code | Meaning |
  |---|---|
  | `empty` | the transcript has no steps |
  | `results_missing` | tool calls with no recorded result |
  | `arguments_missing` | tool calls whose arguments were not recorded |
  | `placeholder_turns` | assistant turns that are a placeholder line (`Executed Bash call_…`) instead of the agent's text. *Heuristic: known placeholder forms.* |
  | `ends_unanswered` | the run stops on a call with no result, so the record may be truncated |
  | `subagents_missing` | a subagent was launched (`Task`, `Agent`, `spawn_agent`, …) but its run is not in the transcript. *Heuristic: by tool name.* |
  | `duplicate_call_ids` | two calls share an id, so results can't be matched to calls reliably |
  | `redactions` | values a filter replaced (`[REDACTED]`) |

- `explanation` gives one line per issue, in `doctor`'s words, with the heuristic ones marked
  `(heuristic)` and the messages they concern cited, for example
  `ends on a call with no result: [M10]`.
- `metadata` holds the counts from `tracelint doctor --json` (`calls`, `results_recorded`,
  `outcome_recorded`, `placeholder_turns`, `subagent_runs`, …), this transcript's `coverage`
  percentages (`results`, `outcome`, `arguments`, `reasoning`), and `heuristic`, which lists the
  codes present that rest on a heuristic.

Scout keeps a subagent it imported, such as Claude Code's `Task`, as an `agent` span in the
transcript's events, linked from the launching call. A launch whose span is there counts as a
recorded run, so a complete Claude Code import is not reported as missing its subagents.

Outcome coverage runs low on every Inspect transcript: Inspect records when a tool fails but never
when it succeeds. That is a measure in the metadata, not an issue.

## What tracelint reads

The scanners read the transcript's messages and its `tool` events (`record_quality` also reads
its `span_begin` events, for subagents). Inspect's chat messages map onto
tracelint's trace:

- System, user and assistant text become messages, and so does assistant reasoning unless it is
  redacted.
- Each `tool_calls[]` entry becomes a tool call, and each `tool` message becomes its result, paired
  by `tool_call_id`.
- A server-side tool use in assistant content (a provider's web search, MCP call or code execution,
  recorded with its result) becomes a call and its result. R13 reads it like any other call, so a
  web search that names an off-limits source is caught.
- Arguments that Inspect could not parse (`parse_error`) are the model's own text. R6 reports them
  as a `hard_defect`.

A tool failed only when a structured signal says so:

- the tool message's `error` (Inspect's `ToolCallError`: a timeout, a parsing failure, a denied
  approval, …);
- the `error` of the call's `tool` event, which is where Scout's importers record a failure (Claude
  Code's `is_error`, a failed span) while leaving the message's field empty;
- a truthy `error` field in a JSON result;
- the line `[error] tool reported failure` that Harbor's Claude Code converter appends to a failed
  result. tracelint's [ATIF reader](atif.md) reads the same line, so a Harbor run gets the same
  verdict either way.

Inspect has no field that says a tool *succeeded*, so any other result is unknown. R2a then reports
error-like output only as a review candidate.

## Validated on real transcripts

All checks below ran Inspect Scout 0.6.0, importing and scanning through `scout import` and
`scout scan tracelint/...`.

- **Harbor (ATIF), 18 trajectories** from the public
  [Terminal-Bench 2.0 leaderboard](https://huggingface.co/datasets/harborframework/terminal-bench-2-leaderboard):
  6 trials each from a Claude Code, a Gemini CLI and a Terminus 2 submission, imported with
  `scout import atif`. On every trajectory, each rule's findings had the same count per tier as
  `tracelint check --format atif` on the same file. That is 72 rule results, with R2a at 19
  `hard_event` and 36 candidates. Without reading Harbor's failure line, the 19 errors arrived only
  as candidates or not at all, because the importer drops the flag behind them.
- **R13 through Scout, 25 trajectories.** These were the Terminal-Bench runs where the integrity
  pilot found answer lookups, plus other trials of the same tasks, scanned with the Terminal-Bench
  `off_limits` list from the [ATIF page](atif.md). Both paths flagged the same 12 runs with
  identical findings. The 7 runs that requested a benchmark source were `hard_defect`; the other 5
  only got one back in a search result (`hard_event`). The citations pointed at the right messages:
  the WebSearch call, then its result.
- **Claude Code, 6 sessions** imported with `scout import claude_code`, from 23 to 7,214 messages
  each. The scan ran with no errors. 145 tool errors came through as `hard_event`; reading the
  messages alone, all of them were candidates or missed. Those failures exist only on the
  transcript's tool events, which is why the scanners load events as well as messages.
- **Record quality.** On the same 43 Harbor trajectories, `record_quality` reported the same issue
  codes as `tracelint.record` run through the native ATIF reader, on every trajectory. On the
  Claude Code sessions, the 12 `Task` subagents Scout kept as agent spans counted as recorded.
  Without the span events, all 12 would have been reported missing.

## Scope

The scanners lint the transcript's message thread. A subagent whose conversation appears only in
the transcript's events (a Claude Code `Task`, an Inspect handoff) is not linted as its own run.
tracelint checks what the trajectory proves. It does not judge whether the agent's answer was
right; Scout's LLM scanners and your scorers do that.
