#!/usr/bin/env python3
"""Run tracelint over a SWE-bench submission's trajectories — Run A (keyless) vs Run B (contract).

Deterministic, zero model calls. Reads downloaded trajectory files + the submission's
``results.json`` (ground truth), converts each via the adapter, lints it both ways, checks Run B is
reproducible, and writes the raw per-trajectory and per-finding CSVs the analysis step consumes. No
trajectory data is committed — point ``--trajs`` at a directory you downloaded (see the README).

    python run.py --submission <name> --trajs <dir> --results <results.json> \
        --tools experiments/swebench/tools.json --out <outdir> [--limit N]
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from swebench.adapter import load_trajectory  # noqa: E402
from tracelint.findings import ConfidenceTier, LintReport  # noqa: E402
from tracelint.rules import default_rules, lint_trace  # noqa: E402
from tracelint.tools import ToolRegistry  # noqa: E402
from tracelint.trace import ResultStatus, ToolCall, ToolResult, Trace  # noqa: E402

#: (rule, tier) pairs tracked as per-trajectory count columns (the ones the study reasons about).
RULE_TIERS = [
    ("R1", "hard_defect"), ("R2a", "hard_event"), ("R2a", "candidate"), ("R2b", "hard_defect"),
    ("R3", "candidate"), ("R4", "candidate"), ("R5", "candidate"), ("R7", "candidate"),
    ("R8", "hard_event"), ("R8", "candidate"),
]


def _finding_key(report: LintReport):
    return sorted(
        (f.rule, f.finding_type, f.tier.value, tuple(f.step_indices), f.summary)
        for f in report.active_findings
    )


def _counts(report: LintReport) -> dict:
    tier_counts = Counter((f.rule, f.tier.value) for f in report.active_findings)
    out = {f"{r}_{t.split('_')[0] if '_' in t else t}": tier_counts.get((r, t), 0)
           for r, t in RULE_TIERS}
    out["exit_code"] = report.exit_code
    out["suppressed"] = len(report.suppressions)
    out["any_hard_defect"] = int(report.has_hard_defect)
    out["any_hard_event"] = int(bool(report.by_tier(ConfidenceTier.HARD_EVENT)))
    out["any_candidate"] = int(bool(report.by_tier(ConfidenceTier.CANDIDATE)))
    return out


def _bash_stats(trace: Trace) -> tuple[int, int, int, int]:
    results = {r.call_id: r for r in trace.steps if isinstance(r, ToolResult)}
    n_calls = sum(1 for s in trace.steps if isinstance(s, ToolCall))
    bash = [s for s in trace.steps if isinstance(s, ToolCall) and s.name == "execute_bash"]
    n_bash = len(bash)
    with_code = nonzero = 0
    for c in bash:
        r = results.get(c.call_id)
        if r is None:
            continue
        if r.status is not ResultStatus.UNKNOWN:
            with_code += 1
        if r.status is ResultStatus.ERROR:
            nonzero += 1
    return n_calls, n_bash, with_code, nonzero


def _finding_rows(report: LintReport, trace: Trace, submission, instance_id, run):
    by_index = {s.index: s for s in trace.steps}
    for f in report.active_findings:
        step = f.step_indices[0] if f.step_indices else -1
        s = by_index.get(step)
        tool = getattr(s, "name", "") if isinstance(s, ToolCall) else ""
        excerpt = ""
        if isinstance(s, ToolResult):
            excerpt = (s.content or "")[:200].replace("\n", " ")
        yield {
            "submission": submission, "instance_id": instance_id, "run": run,
            "rule": f.rule, "tier": f.tier.value, "finding_type": f.finding_type,
            "step": step, "tool": tool,
            "message": (f.summary or "")[:300],
            "evidence_excerpt": excerpt,
        }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--submission", required=True)
    ap.add_argument("--model", default="")
    ap.add_argument("--trajs", required=True, help="directory of <instance_id>.json trajectories")
    ap.add_argument("--results", required=True, help="the submission's results/results.json")
    ap.add_argument("--tools", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    resolved = set(json.loads(Path(args.results).read_text(encoding="utf-8")).get("resolved", []))
    registry = ToolRegistry.load(args.tools)
    rules = default_rules()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    files = sorted(Path(args.trajs).glob("*.json"))
    if args.limit:
        files = files[: args.limit]

    per_traj, per_finding = [], []
    nondeterministic = 0
    n_results = n_results_with_code = 0
    t0 = time.perf_counter()
    for fp in files:
        instance_id = fp.stem
        trace = load_trajectory(str(fp), submission=args.submission, instance_id=instance_id)
        report_a = lint_trace(trace, rules, ToolRegistry())  # keyless
        report_b = lint_trace(trace, rules, registry)  # contract
        if _finding_key(report_b) != _finding_key(lint_trace(trace, rules, registry)):
            nondeterministic += 1
        n_calls, n_bash, n_code, n_nonzero = _bash_stats(trace)
        for r in (s for s in trace.steps if isinstance(s, ToolResult)):
            n_results += 1
            n_results_with_code += int(r.status is not ResultStatus.UNKNOWN)
        row = {
            "submission": args.submission, "model": args.model, "instance_id": instance_id,
            "resolved": int(instance_id in resolved),
            "n_steps": len(trace.steps), "n_tool_calls": n_calls, "n_bash": n_bash,
            "n_bash_with_exit_code": n_code, "n_bash_exit_nonzero": n_nonzero,
        }
        for run, rep in (("A", report_a), ("B", report_b)):
            for k, v in _counts(rep).items():
                row[f"{run}_{k}"] = v
        per_traj.append(row)
        per_finding += list(_finding_rows(report_a, trace, args.submission, instance_id, "A"))
        per_finding += list(_finding_rows(report_b, trace, args.submission, instance_id, "B"))
    elapsed = time.perf_counter() - t0

    if per_traj:
        with (out / "per_trajectory.csv").open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(per_traj[0].keys()))
            w.writeheader()
            w.writerows(per_traj)
    with (out / "per_finding.csv").open("w", newline="", encoding="utf-8") as fh:
        cols = ["submission", "instance_id", "run", "rule", "tier", "finding_type", "step", "tool",
                "message", "evidence_excerpt"]
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows(per_finding)

    def total(run, key):
        return sum(r[f"{run}_{key}"] for r in per_traj)

    print(f"=== {args.submission}: {len(per_traj)} trajectories, {elapsed:.1f}s, 0 model calls ===")
    cov = 100.0 * n_results_with_code / n_results if n_results else 0
    print(f"exit-code coverage: {n_results_with_code}/{n_results} tool results ({cov:.0f}%)")
    det = len(per_traj) - nondeterministic
    print(f"determinism: {det}/{len(per_traj)} Run B identical on repeat")
    print("Run A (keyless) vs Run B (contract) totals:")
    print(f"  suppressed rules : A {total('A','suppressed')}  ->  B {total('B','suppressed')}")
    hd_a, hd_b = total("A", "any_hard_defect"), total("B", "any_hard_defect")
    print(f"  hard_defects     : A {hd_a}  ->  B {hd_b}"
          f"  (R1 {total('B','R1_hard')}, R2b {total('B','R2b_hard')})")
    print(f"  R2a hard_event   : A {total('A','R2a_hard')}  ->  B {total('B','R2a_hard')}")
    print(f"  R2a candidate    : A {total('A','R2a_candidate')} -> B {total('B','R2a_candidate')}")
    print(f"  R7 / R8          : B R7 {total('B','R7_candidate')}, R8 {total('B','R8_candidate')}")
    print(f"wrote {out/'per_trajectory.csv'} and {out/'per_finding.csv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
