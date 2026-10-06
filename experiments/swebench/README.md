# SWE-bench precision + outcome experiment

An **experiment**, not part of tracelint's public API. It lints real agent trajectories from the
SWE-bench leaderboard to measure (1) the precision of tracelint's rules when they fire, and (2)
whether a finding is associated with the run failing (`unresolved`). Deterministic, **zero model
calls**. No third-party trajectory data is committed — the runner reads it from a path.

> Precision / census / outcome-association study, **not** a benchmark claim.

## Status

- **Shipped:** the trajectory → `Trace` adapter (`adapter.py`), the tool contract (`tools.json`),
  and the runner (`run.py`, Run A keyless vs Run B with the contract). Validated on the real 2025
  OpenHands Verified submissions.
- **Next:** the metrics/analysis step — aggregates with Wilson intervals, census, outcome
  association (2×2 vs `resolved`), and the stratified audit sample for hand-labeling. This is where
  the full ~300 MB corpus is downloaded and the numbers are produced.

## Corpus (verified 2026-10-06)

SWE-bench **Verified**, scaffold = **OpenHands**, 3 model submissions (same contract, models vary):

| Submission | Model | Resolved / 500 | Trajectory encoding |
|---|---|---|---|
| `20250524_openhands_claude_4_sonnet` | Claude 4 Sonnet | 352 | OpenAI tool-calls |
| `20250807_openhands_gpt5` | GPT-5 | (from results.json) | OpenAI tool-calls |
| `20250520_openhands_devstral_small` | Devstral-Small (weaker → R7/R1 signal) | 234 | OpenHands text format |

> The handoff named Qwen3-Coder as the weaker model, but it has **no published trajectories** on S3;
> **Devstral-Small** does (234/500, good variance) and is used instead.

Two sources (the handoff's assumptions, corrected):

- **Ground truth** is in the git repo: `evaluation/verified/<submission>/results/results.json` →
  `resolved` (+ `no_generation`, `no_logs`). Record the `experiments` repo commit SHA used.
- **Trajectories are on public S3** (named in each `metadata.yaml` `assets.trajs`):
  `https://swe-bench-submissions.s3.amazonaws.com/verified/<submission>/trajs/<instance_id>.json`
  (~200 KB each, ~100 MB per submission; listable with `?list-type=2&prefix=`). Open with UTF-8.

## Trajectory format (verified, two encodings — both handled)

1. **OpenAI tool-calls** (Claude, GPT-5): action in an assistant `tool_calls[].function.{name,
   arguments(JSON)}`; observation in a paired `tool` message by `tool_call_id`.
2. **OpenHands text** (Devstral): action in the assistant text as
   `<function=NAME><parameter=KEY>VALUE</parameter></function>`; observation in the *next* `user`
   message (`EXECUTION RESULT of [NAME]: …`). Array/object params (e.g. `view_range`) are JSON-parsed
   so they are typed like the OpenAI form.

In both, the **exit code is in the observation text** (`[The command completed with exit code N.]`),
extracted by one regex (last marker = final status). Tools: `execute_bash`, `str_replace_editor`
(split by its `command` into `view`/`create`/`str_replace`/`insert`), `think`.

## Adapter (`adapter.py`)

`run_id = "<submission>/<instance_id>"`. user → user `Message`; assistant text and `think` →
thought `Message`; each other tool call → `ToolCall` (`str_replace_editor` split by command so a
write can be side-effecting, a view not); observation → `ToolResult` with `status` from the exit
code (`0→ok`, `≠0→error`, absent→`unknown`), so R2a fires on the structured status.

## Contract (`tools.json`)

Registry = exactly these tools → anything else is **R7**. `execute_bash` and `str_replace_editor.view`
are **not** side-effecting; `str_replace_editor.{str_replace,insert,create}` are (`idempotent:false`)
— enabling **R8** (duplicate edit) and **R2b** (a failed command's value reused in an edit). Schemas
carry required args for **R1**. `execute_bash` is not side-effecting in v1 on purpose (side effects
are per-command — `cat`/`pytest` are reads, `git commit` is a write — and a static contract can't
split a shell string; marking it true would make R8/R5 over-fire on legit repeated `pytest`).

## Runner (`run.py`)

```bash
python experiments/swebench/run.py \
  --submission 20250524_openhands_claude_4_sonnet --model claude-4-sonnet \
  --trajs <downloaded_trajs_dir> --results <results.json> \
  --tools experiments/swebench/tools.json --out <outdir>
```

Per trajectory it lints **Run A** (keyless) and **Run B** (contract), checks Run B is reproducible,
and writes `per_trajectory.csv` (metadata + `resolved` + per-rule/tier counts for A and B) and
`per_finding.csv` (every finding with its step, tool, message, evidence excerpt — for the audit and
for quotable examples). It prints exit-code coverage, the determinism check, and the A→B before/after
totals. Zero model calls; wall-clock is reported.

## v1 limitations (for the post)

- Exit-code coverage is **reported, not assumed**; observations without a marker map to `unknown`
  (fail-closed — never silently "ok").
- The text-format parser is validated on samples; the stratified **audit sample** validates finding
  precision per rule (including any residual parser edge cases) before any number is published. (In
  the Devstral sample, R1 correctly caught real `create` calls the weak model emitted with no
  `file_text` — a genuine malformed edit, not a parser artifact.)
- Precision + association, **not recall**: most failed runs fail on *reasoning*, not structure, so
  recall vs `unresolved` is expected to be modest; precision-when-a-rule-fires is the number.

## Reproduce / tests

`pytest tests/test_swebench_adapter.py tests/test_swebench_run.py` (synthetic fixtures of both
encodings — no data needed).
