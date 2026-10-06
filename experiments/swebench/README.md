# SWE-bench precision + outcome experiment

An **experiment**, not part of tracelint's public API. It lints real agent trajectories from the
SWE-bench leaderboard to measure (1) the precision of tracelint's rules when they fire, and (2)
whether a finding is associated with the run failing (`unresolved`). Deterministic, **zero model
calls**. No third-party trajectory data is committed here — the code reads it from a path; see
*Corpus* below for the download.

> Precision / census / outcome-association study, **not** a benchmark claim.

## Status

- **This PR:** the trajectory → `Trace` adapter (`adapter.py`) + tests. Validated on the real 2025
  OpenHands Claude-4-Sonnet and GPT-5 Verified submissions.
- **Next PRs:** the tool contract (`tools.json`), the runner (Run A keyless vs Run B with contract),
  and the metrics emitter (per-trajectory / per-finding CSVs, aggregates with Wilson intervals,
  outcome association, audit sample).

## Corpus (verified 2026-10-06)

SWE-bench **Verified**, scaffold = **OpenHands**, 2–3 model submissions (same contract, models vary):

| Submission | Model | Resolved / 500 |
|---|---|---|
| `20250524_openhands_claude_4_sonnet` | Claude 4 Sonnet | 352 |
| `20250807_openhands_gpt5` | GPT-5 | (read from results.json) |
| `20250805_openhands-Qwen3-Coder-30B-A3B-Instruct` | Qwen3-Coder-30B (weaker → R7 signal) | (read from results.json) |

Two sources, which the handoff got slightly wrong — **corrected here**:

- **Ground truth** is in the `experiments` git repo:
  `evaluation/verified/<submission>/results/results.json` → `resolved` (plus `no_generation`,
  `no_logs`). That is the `resolved` / `unresolved` label per instance.
- **Trajectories are NOT in the git repo** (it keeps only `results/`). They live in a **public S3
  bucket** named in each `metadata.yaml` under `assets.trajs`:
  `s3://swe-bench-submissions/verified/<submission>/trajs/<instance_id>.json`
  (HTTP: `https://swe-bench-submissions.s3.amazonaws.com/verified/<submission>/trajs/<id>.json`,
  one ~200 KB JSON per instance, ~100 MB per submission).

Record the submission names and the `experiments` repo commit SHA used for `results.json` when
running (the post must be reproducible).

## Trajectory format (verified, not assumed)

Each trajectory is an **OpenAI chat message list**, not OpenHands event objects:

- `assistant` messages carry actions in `tool_calls[].function.{name, arguments(JSON string)}` and
  thoughts in `content` text parts;
- each paired `tool` message carries the observation, matched by `tool_call_id`;
- the **exit code is in the observation text** — e.g. `[The command completed with exit code 0.]` —
  **not** a structured `CmdOutputObservation.exit_code` field. It is extracted with one regex
  (`exit code (-?\d+)`, last marker = the final command's status). Coverage was 17/17 on bash
  observations in the Claude sample; the runner reports it per corpus.
- Tools seen: `execute_bash` (`{command, timeout?}`), `str_replace_editor` (one tool, `command`
  discriminator ∈ view/create/str_replace/insert, + `path`/`view_range`/`old_str`/`new_str`/
  `file_text`), `think` (`{thought}`).

## Adapter mapping (`adapter.py`)

One trace per trajectory, `run_id = "<submission>/<instance_id>"`:

- `user` message → user `Message` (the issue; later continuation prompts kept too).
- assistant text, and any `think` call → assistant `Message` (so R3's provenance sees the reasoning).
- every other tool call → `ToolCall` (`call_id` = the OpenAI `tool_calls[].id`), args = parsed
  `arguments`.
- its observation → `ToolResult` (full text kept as `content` for audit) with `status` from the
  exit code: **`0 → ok`, `≠0 → error`, absent → `unknown`**. R2a then fires on the structured
  status (a nonzero exit is a `hard_event` tool error), not a string match.

## Reproduce the adapter

```bash
# one trajectory (public S3, no auth):
curl -L "https://swe-bench-submissions.s3.amazonaws.com/verified/20250524_openhands_claude_4_sonnet/trajs/astropy__astropy-12907.json" -o traj.json
python -c "import sys; sys.path.insert(0,'experiments'); from swebench.adapter import load_trajectory; \
t=load_trajectory('traj.json', submission='20250524_openhands_claude_4_sonnet', instance_id='astropy__astropy-12907'); \
print(t.run_id, len(t.steps), 'steps')"
```

Tests: `pytest tests/test_swebench_adapter.py` (synthetic fixture of the verified shape; no data
needed).

## v1 limitations (to document in the post)

- `execute_bash` and `str_replace_editor` are declared **not** side-effecting in v1: side effects are
  *per command* (`cat`/`pytest`/`view` are reads, `str_replace`/`git commit` are writes) and a
  static contract can't split them. Marking them side-effecting would make R8/R5 over-fire on legit
  repeated `pytest`. R8-on-shell is out of scope for v1.
- Exit-code coverage is reported, not assumed; observations without a marker map to `unknown`
  (fail-closed — never silently "ok").
- This is precision + association, not recall. Most failed runs fail on *reasoning*, not structure,
  so recall against `unresolved` is expected to be modest; the number that matters is precision when
  a rule fires.
