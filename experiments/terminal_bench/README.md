# Terminal-Bench 2.0 loop study

An **experiment**, not part of tracelint's public API. It lints every public Terminal-Bench 2.0
leaderboard trial that ships a Harbor ATIF trajectory, to ask what the
[SWE-bench study](../swebench/README.md) could not: **which agent harnesses stop a stuck agent, and
how much do the others spend after it is stuck?** Deterministic, keyless, **zero model calls**. No
trajectory data is committed.

## Why this follow-up

The SWE-bench study found that a loop (R4) went with a failed run 90% of the time. Its data came from
one harness, OpenHands, which ships a built-in loop breaker (`StuckDetector`: 4 identical
action/result pairs, or 3 identical action→errors, halt the agent). So its loops were short (median 3
repeats) and the "loop → failure" association partly measured "the harness halted the run". The
leaderboard has many harnesses, some without a breaker, so the question becomes measurable.

## Corpus (verified 2026-10-08)

The leaderboard's own dataset, [`harborframework/terminal-bench-2-leaderboard`][hf] (Apache-2.0):
every submission is a Harbor job, `submissions/terminal-bench/2.0/<Agent__Model>/<job>/<trial>/`,
89 tasks × 5 trials. Each trial has the grader's `result.json` (reward, and `exception_info` —
`AgentTimeoutError` when the harness killed the agent at the task's time limit) and, for **23 of 75
submissions**, an ATIF `agent/trajectory.json`. The rest publish native logs in other formats, or
none, and are out of scope.

[hf]: https://huggingface.co/datasets/harborframework/terminal-bench-2-leaderboard

## Pipeline

```bash
DATA=./tb2_data   # outside the repo; ~10k trials, trajectories stored gzipped
python experiments/terminal_bench/fetch.py --all-atif --out "$DATA"
python experiments/terminal_bench/run.py --data "$DATA" --out runs --workers 8
```

`fetch.py` downloads only each trial's `agent/trajectory.json` (gzipped) and `result.json`, skips
trials already present, and backs off on rate limits (`HF_TOKEN` raises them; not required).

`run.py` reads each trajectory with tracelint's ATIF adapter (`--format atif`), lints it keyless,
re-lints every 10th trial to check determinism, and writes:

- `per_trial.csv` — submission, harness, model, task, reward/resolved, exception/timed-out, agent
  wall-clock, turns, calls, findings per rule/tier, and loop summaries (count, longest streak,
  whether a loop was still running when the run ended, what came after the first one).
- `per_loop.csv` — one row per R4 loop: the repeated call and its output, streak length, the
  *detection point* (the 3rd identical call, the earliest a live check could know), and the agent
  turns, wall-clock seconds, and tokens (where recorded) after it.
- `per_finding.csv` — every active finding.

## What each loop is tagged with, and why

R4 groups calls by tool, arguments, and a **coarse** result class — any non-empty, non-error output
is `ok`. So three identical calls whose output *changed* (a growing log, a progress bar) still read
as a loop. The runner records what R4 can't see, which is the audit's ground truth:

- `distinct_results` / `distinct_results_norm` — how many different outputs the repeats returned,
  exactly and ignoring numbers, hex ids, and whitespace (a timestamp, counter, or a poll's random
  chunk id isn't progress). `1` is a genuinely stuck repeat.
- `is_wait` — every text argument empty (Terminus's `keystrokes: ""`) or a `sleep` / `wait`: polling
  something that is running.
- `advanced_later` — the same call returned something different later in the run.
- `runs_to_end` — the run ended inside the loop; with `timed_out`, it ran until the harness killed it.

## Analysis (`analyze.py`)

```bash
python experiments/terminal_bench/analyze.py --runs runs --out results
```

Each loop is classified by what its repeats actually returned:

| Kind | Meaning |
|---|---|
| `stuck` | the same action, the same output (ignoring numbers, ids, whitespace): no progress |
| `wait-silent` | a poll whose output didn't change, and the same poll never returned anything new |
| `wait-advanced` | a poll whose output didn't change in the streak, but returned something new later |
| `changing` | the output differed between repeats — progress R4's coarse result class missed |

Per submission (one harness + model) and per harness, `aggregates.json` reports: the share of trials
with a stuck loop; stuck-loop length (median / p90 / max — a harness with a loop breaker caps it);
how often the run ended inside a stuck loop and whether by the time limit; P(run failed | stuck loop)
against the base rate with Wilson 95% intervals; and, in runs that failed anyway, the agent turns and
wall-clock spent after the stuck loop first became detectable — per run, and as a share of all
failed-run wall-clock. `audit_sample.csv` draws up to 5 loops per (submission, kind) with an empty
`label` column, to check the classification by hand.

## Results (first full run, 2026-10-08)

10,541 trials (23 submissions, 13 harnesses), zero model calls, 1,055/1,055 re-linted trials
identical. Base failure rate 35.7%; 16% of trials hit the harness time limit.

**R4 flagged 2,622 loops; almost none are an agent stuck.** By what the repeats actually returned:

| Kind | Loops | |
|---|---|---|
| `changing` | 1,448 (55%) | the output progressed (training logs, compiler output, rising CPU time) |
| `unrecorded` | 681 (26%) | every repeat had no result: Terminus-style batches, server-side `web_search` |
| `wait-advanced` / `wait-silent` | 339 / 136 (18%) | polls of a running process |
| `stuck` | 18 (0.7%) | the same action, the same output |

A hand audit of all 18 `stuck` loops found 15 genuinely stuck — e.g. `make` × 494 with no makefile
until the 900 s timeout, a nonexistent tool called 4×, a required argument missing 4×, the same
failing test re-run 8× with no edit between. The other 3 were deliberate repeated measurements
(benchmark timings, simulation scores) whose outputs differ only in digits; all 13 loops with
*exactly* identical output were stuck. So on terminal agents R4 as designed is ~0.6% precise: its
coarse result class (any non-empty output is `ok`) reads progress and polling as "no change", and a
missing result reads as an identical one.

**Stuck loops are rare and cheap here.** 17 trials of 10,541 (0.16%) have one, in 6 submissions.
Only 3 of 18 were still running when the run ended (2 by timeout). In the runs that failed anyway,
the agent spent a median 36 turns / 254 s after the loop became detectable — but that is 0.2% of all
failed-run wall-clock. No harness shows a pattern of runaway loops; the one runaway is Claude Code +
GLM-4.7.

**Outcome.** P(run failed | stuck loop) = 0.65 [0.41, 0.83], n = 17, vs a 0.36 base (lift 1.8): the
same direction as the SWE-bench study, on a small sample.

**Follow-up: R4 fixed.** These results drove an R4 change (see CHANGELOG): it now compares the
repeats' results exactly, treats a missing result as unknown, and counts a poll only if the trace
ended while it was still waiting. Re-run on this corpus, R4 flags **31 loops instead of 2,622** — 19
identical repeats (17 stuck on review, 2 an unchanged `tail` of a log) and 12 polls still waiting
at the end — with no `changing` or `unrecorded` loops left. On the SWE-bench corpus it keeps 28 of
the 29 loop runs (P(unresolved) 89%), so that study's loops were identical repeats.

## Limitations

- Association, not causation. Keyless only: no per-harness tool contract, so contract rules
  (R1/R7/R8/R9-R12) don't run.
- One harness's run can embed subagents; only the root trajectory is linted per trial.
- Tokens and cost are recorded by only some harnesses; turns and wall-clock (step timestamps) are
  the universal units.
- Terminus records one terminal screen per batch of keystrokes, so earlier calls in a batch have no
  result of their own (see `docs/integrations/atif.md`).

Tests (no data needed): `pytest tests/test_terminal_bench_run.py`.
