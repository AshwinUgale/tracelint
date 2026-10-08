# tracelint + Harbor (ATIF)

[Harbor](https://github.com/harbor-framework/harbor) runs agents on tasks in sandboxes (it is the
harness behind Terminal-Bench) and records every run as an **ATIF** trajectory — the Agent
Trajectory Interchange Format, `agent/trajectory.json` in each trial folder. ATIF is the same JSON
whichever agent produced it (Claude Code, Codex, Gemini CLI, OpenHands, Terminus, …), so one reader
covers every agent Harbor runs.

## Lint a Harbor job

A job holds one folder per trial, each with its trajectory:

```bash
tracelint check jobs/<job>/*/agent/trajectory.json --format atif
```

Each trajectory is its own run; a subagent embedded in one (`subagent_trajectories`) is linted as a
separate run named `<parent run>/<its trajectory_id>`. Read without `--format atif`, a trajectory is
an input error (exit 3) that names the right flag — never a clean pass.

From Python:

```python
import json
from tracelint import atif_tools_to_registry, default_rules, from_atif_trajectories, lint_trace

doc = json.load(open("agent/trajectory.json", encoding="utf-8"))
registry = atif_tools_to_registry(doc)  # the agent's own tool definitions, when recorded
for trace in from_atif_trajectories(doc):
    print(lint_trace(trace, default_rules(), registry).active_findings)
```

## What tracelint reads

- `system` / `user` steps → messages; an `agent` step's `reasoning_content` and `message` →
  assistant messages (never a source a hallucination check accepts).
- Each `tool_calls[]` entry → a tool call; each `observation.results[]` entry with a
  `source_call_id` → that call's result.
- A result with no `source_call_id` is what the agent was shown, not a tool result — except the one
  observation a turn records for its whole batch of calls (Harbor's **Terminus** records one
  terminal screen per batch of keystrokes), which is paired with the turn's last call. Earlier calls
  in the batch get no result of their own: the agent never saw one.
- Steps marked `is_copied_context` (copied in after a context summary) are context, not new actions:
  their text is kept, their tool calls are not replayed — replaying them would read as the agent
  repeating itself.
- `agent.tool_definitions` (OpenAI function format) are attached to each call as its schema, so
  `tracelint init trajectory.json --format atif` drafts a `tools.json` and R11 can compare.

## Did the tool fail? (result status)

ATIF has no result-status field, so each producer marks a failed tool its own way. tracelint reads
these structured signals and nothing else — never a guess from the text:

| Signal on the observation result | Who writes it | Read as |
|---|---|---|
| `extra.is_error: true` | Harbor's Kimi and Pi converters | failed |
| `extra.tool_result_is_error: true` | Harbor's Claude Code converter | failed |
| a line `[error] tool reported failure` at the end of `content` | Harbor's Claude Code converter | failed |
| `extra.status: "error"` / `"failed"` | Strands | failed |
| a truthy `error` field in the result itself | any tool | failed |
| `extra.is_error: false`, `extra.status: "success"` | as above | succeeded |
| `extra.exit_code` / `return_code` / `returncode` of `0` | some submitters | succeeded |
| anything else, including a **non-zero exit code** | | unknown |

A non-zero exit code stays *unknown* on purpose: `grep` with no match, or a reproduction script that
is meant to fail, exits non-zero on a run that is going fine. An unknown result makes R2a fall back
to its text heuristic, as a review-only candidate, rather than assert an error. If you produce ATIF,
`extra.is_error` on each observation result is the signal that turns R2a into a certain fact.

## Validated on real trajectories

The adapter was run on real trajectories from the public
[Terminal-Bench 2.0 leaderboard](https://huggingface.co/datasets/harborframework/terminal-bench-2-leaderboard)
(one trial from each ATIF-producing submission: 23 trajectories from 21 harnesses, ATIF v1.2–v1.6).
All 23 load; 520 of their 643 tool calls are paired with a result (the rest are earlier calls in a
Terminus batch); three submissions whose `trajectory.json` is a custom, non-ATIF format are rejected
with an input error rather than read as empty.

## Scope

tracelint reads what the trajectory proves; it does not replace Harbor's verifiers, which grade
whether the task was solved. A producer that writes its actions inside message text rather than as
`tool_calls` has nothing for the call-based rules to check.
