# Record quality: can these traces be trusted?

tracelint's rules judge what an agent did. Before that, it's worth asking whether the trace records
what the agent did. A dashboard, an LLM judge or a rule downstream inherits every gap silently:

- a tool call whose result was never recorded looks like a call that returned nothing;
- a placeholder where the reasoning should be looks like an agent that said nothing;
- a subagent whose run wasn't exported looks like work that never happened.

`tracelint doctor` measures the record itself. These checks never count against the agent.

```bash
tracelint doctor runs/**/trajectory.json --format atif --group-by dir
tracelint doctor traces/*.json --json record.json            # per-trace checks for other tools
tracelint doctor traces/*.json --fail-under results=90        # gate instrumentation in CI (exit 1)
```

## What it measures

| Measure | Meaning |
|---|---|
| results recorded | % of tool calls with a recorded result, paired to the call |
| outcome recorded | % of recorded results that say whether they succeeded (an `is_error` flag, a status, an exit code) |
| arguments recorded | % of tool calls whose arguments were recorded |
| reasoning recorded | % of assistant turns with real content, not a placeholder (`Executed exec_command call_…`, `Tool call`) or empty text. *Heuristic: matched on known placeholder forms.* |

And per trace:

| Check | Meaning |
|---|---|
| ends on an unanswered call | the record stops on a tool call with no result, so it may be truncated |
| subagent work missing | the run launched a subagent (`Task`, `Agent`, `spawn_agent`, `run_subagent`, …) but the subagent's run isn't in the batch. *Heuristic: by tool name; a subagent run is matched as `<run_id>/<child>`.* |
| duplicate call ids | two calls share an id, so results can't be matched to calls reliably |
| redacted values | `[REDACTED]` / `<REDACTED_…>` markers: values a filter replaced, so analysis of them is unreliable |
| empty traces, unreadable files | nothing usable. `doctor` lists each unreadable file and keeps going. |

In [Inspect Scout](integrations/inspect-scout.md#record-quality-judging-the-record-not-the-agent),
`scout scan tracelint/record_quality` runs the same checks on each transcript it scans or imports.

`--group-by dir` breaks the measures down by the first folder below the inputs' common root, which
is usually one per harness or per run. `--group-by model` breaks them down by the model the trace
records.

## What real traces look like

Here are the Terminal-Bench 2.0 leaderboard runs: 10,541 traces, 493,553 tool calls, 23
submissions across 13 harnesses.

| Measure | All runs | Range across submissions |
|---|---:|---|
| results recorded | 67.1% | 15% to 100% (the Terminus family records one result per batch of commands) |
| outcome recorded | 3.5% | 0% for most; 90% for one harness that records exit codes |
| arguments recorded | 100% | one harness records no structured tool calls at all |
| reasoning recorded | 75.1% | 19% to 100% (several harnesses write placeholder lines) |

Per run: 173 runs end on an unanswered call, and 456 launched subagents whose runs aren't in the
export. Claude Code's `Task`/`Agent` explorers and `spawn_agent` are examples. Separately, 76 runs
carry redacted values, and 18 have duplicate call ids.

An outcome-recorded rate near zero means nothing downstream, whether rule, dashboard or judge, can
tell a failed tool call from a successful one without guessing from the text.

## What it can't see

History that a producer dropped without leaving a marker (context compaction, say) leaves an
internally consistent record, and no check here finds it. In one audited case, an agent's exported
trajectory had 99 steps while its native session log had 3,786 messages. The fix belongs to whoever
writes the trace: record a marker when history is compacted or truncated.

## Gating

`doctor` is advisory and exits 0. `--fail-under NAME=PERCENT` (repeatable; NAME is one of
`results`, `outcome`, `arguments` or `reasoning`) exits 1 when a measure across the traces is below
the threshold. Use it to keep instrumentation from silently degrading. A measure with nothing to
measure (no tool calls, say) never fails.
