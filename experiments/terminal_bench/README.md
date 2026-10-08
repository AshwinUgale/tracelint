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
  exactly and ignoring numbers and whitespace (a timestamp or counter isn't progress). `1` is a
  genuinely stuck repeat.
- `is_wait` — every text argument empty (Terminus's `keystrokes: ""`) or a `sleep` / `wait`: polling
  something that is running.
- `advanced_later` — the same call returned something different later in the run.
- `runs_to_end` — the run ended inside the loop; with `timed_out`, it ran until the harness killed it.

## Limitations

- Association, not causation. Keyless only: no per-harness tool contract, so contract rules
  (R1/R7/R8/R9-R12) don't run.
- One harness's run can embed subagents; only the root trajectory is linted per trial.
- Tokens and cost are recorded by only some harnesses; turns and wall-clock (step timestamps) are
  the universal units.
- Terminus records one terminal screen per batch of keystrokes, so earlier calls in a batch have no
  result of their own (see `docs/integrations/atif.md`).

Tests (no data needed): `pytest tests/test_terminal_bench_run.py`.
