# Precision on real agent trajectories (SWE-bench ecosystem)

A deterministic linter lives or dies on **false positives** — a tool that cries wolf on correct runs
gets muted. So the question that matters isn't "does tracelint find things," it's "when it fires on
a run that was actually fine, how often is it wrong?" This is a **precision / over-fire probe** on
real agent trajectories. (It is not a recall test — the runs aren't labeled for structural defects,
so it says nothing about what tracelint *misses*.)

## TL;DR

On **200 real, verified-correct agent trajectories** (5,496 tool calls):

| | Result |
|---|---|
| **CI-gating findings** (`hard_defect` / `hard_event`) | **0 / 200** — tracelint never fails a correct run's CI |
| **Loop rule (R4)** | **1 / 200** — and that one is a real 3×-identical no-op, not a retry (25/200 runs repeated an identical call ≥3×, so it had ample chances to over-fire) |
| Keyless candidate heuristics (R3, R2a) | **noisy** on raw shell/stdout — a real, named limitation (see below), now partly fixed |

So the gate is clean and the retry-sensitive structural rule behaves. The honest catch is the
*keyless candidate* tier — which this experiment then drove a fix into.

## The data (be precise)

- **Source:** [`nanoswe/nanoswe-trajs-261002`](https://huggingface.co/datasets/nanoswe/nanoswe-trajs-261002)
  on HuggingFace — **mini-swe-agent** runs on **swe-smith** tasks. These are SWE-bench-*family*
  (swe-smith is the SWE-bench authors' synthetic-bug task generator); **not the original SWE-bench
  Verified issues.** One row = one run.
- **Sample:** 200 rows with `verified=True` (the run's patch resolved the task) and
  `exit_status=Submitted`, seed-fixed random from one 7,964-row shard. 5,496 tool calls. Because
  every run succeeded, **a firing is a candidate false positive.**

## Method

- **Mapping:** each assistant tool call `{"name":"bash","arguments":{"command":...}}` → a `ToolCall`;
  the following user message (`<returncode>…<output>…`) → its `ToolResult`, with **`status` left
  `UNKNOWN`** — we deliberately never pre-label an error, so tracelint's own heuristics decide. The
  issue text is seeded as a user message so R3's provenance isn't starved.
- **Keyless:** no `tools.json`, so R1/R7 suppress and the side-effect rules (R2b/R8–R12) stay
  dormant. This exercises exactly the rules that run with no contract: **R2a, R3, R4, R5.**
- **Ground truth tracelint never saw:** each observation carries a `<returncode>`. We kept it *out*
  of what tracelint reads and used it only to *grade* the findings afterward — if a command returned
  exit 0, the tool succeeded, so an R2a "tool error" on it is a provable false positive.

## Results

**Gating & loops (the headline):**

```
exit codes: {0: 200}          # every run passes
hard_defect: 0/200   hard_event: 0/200
retries present: 76/200 repeat an identical call ≥2×, 25/200 ≥3× (R4's threshold)
R4 (loop): 1/200     # the one hit: "bash called 3× in a row, identical args, no change" — a real no-op
R5 (redundant): 39/200, 81 findings (1.5 per 100 calls) — mostly benign file re-reads
```

**Findings quality (using `<returncode>` as ground truth):**

- **R2a (tool error): 62% provable false positives.** Of 1,869 findings, **1,150 flagged a command
  that returned exit 0** — e.g. `sed -n '220,280p' create_contexts.py` exits 0 but is flagged
  because the *source it printed* contains the word "error." Another 27% are exit 1 (grep-no-match,
  a failing test being fixed — legit data, not a tool malfunction). Mechanism: keyless R2a matches
  error-like strings in result content, and shell stdout/stderr is full of them.
- **R3 (hallucinated arg): fired on ~95% of calls — no discriminating power.** It had no provenance
  model for a free-form shell command, so it flagged nearly all of them.

## What this experiment fixed

- **R3 — skip free-form text arguments** ([the fix](../rules.md#r3)). A composed command/query is
  *generated* by the model, not *derived* from observation, so R3 now skips a string of more than
  one token on the keyless candidate path (a discrete datum — id, number, filename — is a single
  token), unless the field is annotated `provided`. **R3 findings dropped ~88% on this sample
  (5,779 → 684)** with no change to its discrete-value catches.
- **R2a — no change needed; it was already correct.** R2a already trusts an explicit success status
  (`if result.status is ResultStatus.OK: continue`) *before* its string heuristic. The false
  positives came from the mapping leaving `status` unset, i.e. an **adapter gap, not a rule defect.**
  Mapping the structured signal conservatively — **`exit 0 ⇒ OK`, and never `nonzero ⇒ error`**
  (grep-no-match and failing tests exit nonzero legitimately — treating that as an error is the
  "data is not a defect" trap) — cuts R2a false positives ~61% (−1,150) with **0** new hard-fires.
  The lesson is adapter-side: give tracelint the structured result status the trace already carries.

## Honest scope & limits

- **Precision, not recall.** Unlabeled for structural defects → this measures false positives, not
  misses.
- **swe-smith ≠ SWE-bench Verified.** Real agent runs on SWE-bench-*family* tasks.
- **Keyless is not the product.** The rules that *gate CI* (R1, R2b, R8, R9) need a `tools.json`
  declaring schemas and `side_effecting` / `failure_when` / `requires`; they were dormant here. The
  real value on these traces comes from a contract, not from the keyless candidate heuristics.
- **Takeaway:** don't point tracelint keyless at free-form shell-agent traces and read the candidate
  output as verdicts. Run R4/R5 (which behave), give it the structured result status, and declare a
  contract for the gating rules.

## Reproduce

Scripts in [`swe-bench/`](swe-bench/):

```bash
pip install "tracelint" pandas pyarrow

# the full run (downloads one ~88 MB shard from the HF dataset above):
curl -L https://huggingface.co/datasets/nanoswe/nanoswe-trajs-261002/resolve/main/train-00000.parquet -o shard.parquet
python swe-bench/run_precision.py shard.parquet 200        # gating + per-rule rates
python swe-bench/findings_quality.py shard.parquet 200     # returncode-grounded FP analysis

# offline taste on the classic SWE-agent .traj format (no download):
python swe-bench/traj_to_tracelint.py swe-bench/sample.traj sample.native.json
tracelint check sample.native.json --include-candidates
```
