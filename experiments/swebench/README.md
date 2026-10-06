# SWE-bench precision + outcome experiment

An **experiment**, not part of tracelint's public API. It lints real agent trajectories from the
SWE-bench leaderboard to measure (1) the precision of tracelint's rules when they fire, and (2)
whether a finding is associated with the run failing (`unresolved`). Deterministic, **zero model
calls**. No third-party trajectory data is committed — the runner reads it from a path.

> Precision / census / outcome-association study, **not** a benchmark claim.

## Status

The full pipeline is in: `fetch.py` (download), `adapter.py` (trajectory → `Trace`), `tools.json`
(contract), `run.py` (Run A keyless vs Run B with the contract), and `analyze.py` (aggregates with
Wilson intervals, census, before/after, outcome association, and the stratified audit sample). The
remaining step is **human**: label the audit sample TP/FP, which turns the census into precision.

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

Registry = the **complete** OpenHands toolset enumerated across all 1,494 trajectories:
`execute_bash`, `str_replace_editor.{view, create, str_replace, insert, undo_edit}`, and `think`
(mapped to a thought). Anything else is **R7** — a genuinely hallucinated tool. `execute_bash` and
`…view` are **not** side-effecting; `…{str_replace, insert, create, undo_edit}` are (`idempotent:false`)
— enabling **R8** (duplicate edit) and **R2b** (a failed command's value reused in an edit). Schemas
carry required args for **R1**; the editor `command` is a `const` per sub-tool, so R1 still validates
it while **R3 skips it** (it's a fixed keyword, not a value to derive). `execute_bash` is not
side-effecting in v1 on purpose (side effects are per-command — `cat`/`pytest` are reads, `git commit`
is a write — and a static contract can't split a shell string; marking it true would make R8/R5
over-fire on legit repeated `pytest`).

> Note from the first full run: **R7 = 0 across the corpus** — these agents never hallucinated a tool
> (OpenHands constrains the toolset via structured tool-calling). The initial R7 hits were a single
> real editor command (`undo_edit`) missing from the registry, now added — an honest null result for
> R7 on this scaffold.

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

## Analysis (`analyze.py`)

Reads every `per_trajectory.csv` / `per_finding.csv` under `--runs` and writes `aggregates.json` +
`audit_sample.csv`:

- **exit-code coverage** (overall + per model);
- **census** — per model, per rule/tier, the fraction of trajectories with ≥1 finding, each with a
  Wilson 95% interval;
- **before/after** — Run A (keyless) → Run B (contract): suppressed, hard-defect trajectories, R2a
  hard vs candidate;
- **outcome association** — for each predicate (`any_hard_defect`, `any_hard_event`, `R2a_hard`, `R4`,
  `R8`, `R7`): P(unresolved | fired) vs P(unresolved | not fired), lift, and recall — does a finding
  predict the run *failing*? Overall and per model;
- **`audit_sample.csv`** — a stratified sample (up to N per rule, Run B + Run A's R2a) with an empty
  `label` column for the hand audit. Labeling it TP/FP is what turns the census into **precision**.

## Reproduce the whole study

```bash
DATA=./swebench_data   # outside the repo; ~274 MB for the three submissions
for S in 20250524_openhands_claude_4_sonnet 20250807_openhands_gpt5 20250520_openhands_devstral_small; do
  python experiments/swebench/fetch.py --submission "$S" --out "$DATA"
  python experiments/swebench/run.py --submission "$S" --trajs "$DATA/$S/trajs" \
    --results "$DATA/$S/results.json" --tools experiments/swebench/tools.json --out runs/"$S"
done
python experiments/swebench/analyze.py --runs runs --out results
```

Tests (no data needed — synthetic fixtures of both encodings):
`pytest tests/test_swebench_adapter.py tests/test_swebench_run.py tests/test_swebench_analyze.py`.
