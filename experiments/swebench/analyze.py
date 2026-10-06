#!/usr/bin/env python3
"""Aggregate the runner's CSVs into the study's metrics + the audit sample.

Reads every ``per_trajectory.csv`` / ``per_finding.csv`` under ``--runs`` and emits:
exit-code coverage, a per-rule/tier census (with Wilson 95% intervals), the Run A -> Run B
before/after, the outcome association (does a finding predict an *unresolved* run?), and a
stratified ``audit_sample.csv`` (empty ``label`` column) for hand-labeling TP/FP. No model calls.

    python experiments/swebench/analyze.py --runs <runs_dir> --out <outdir>
"""

from __future__ import annotations

import argparse
import csv
import json
import random
from collections import defaultdict
from pathlib import Path

from tracelint.stats import wilson_interval

#: Per-trajectory count columns (per run prefix), matching run.py's _counts.
RULE_TIER_COLS = [
    "R1_hard", "R2a_hard", "R2a_candidate", "R2b_hard", "R3_candidate",
    "R4_candidate", "R5_candidate", "R7_candidate", "R8_hard", "R8_candidate",
]
#: Per-trajectory boolean predicates (Run B) tested against the run being unresolved.
PREDICATES = {
    "any_hard_defect": lambda r: r["B_any_hard_defect"] > 0,
    "any_hard_event": lambda r: r["B_any_hard_event"] > 0,
    "R2a_hard": lambda r: r["B_R2a_hard"] > 0,
    "R4": lambda r: r["B_R4_candidate"] > 0,
    "R8": lambda r: r["B_R8_hard"] + r["B_R8_candidate"] > 0,
    "R7": lambda r: r["B_R7_candidate"] > 0,
}


def _read_csvs(runs: Path, name: str) -> list[dict]:
    rows = []
    for fp in sorted(runs.rglob(name)):
        with fp.open(encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                rows.append({k: (int(v) if v.lstrip("-").isdigit() else v) for k, v in row.items()})
    return rows


def _rate(k: int, n: int) -> dict:
    lo, hi = wilson_interval(k, n) if n else (0.0, 0.0)
    return {"k": k, "n": n, "rate": round(k / n, 4) if n else 0.0,
            "ci95": [round(lo, 4), round(hi, 4)]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--audit-per-rule", type=int, default=50)
    ap.add_argument("--seed", type=int, default=20261006)
    args = ap.parse_args()

    traj = _read_csvs(Path(args.runs), "per_trajectory.csv")
    findings = _read_csvs(Path(args.runs), "per_finding.csv")
    models = sorted({r["model"] or r["submission"] for r in traj})
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    report: dict = {"n_trajectories": len(traj), "models": models}

    # 1. exit-code coverage (overall + per model)
    def coverage(rows):
        bash = sum(r["n_bash"] for r in rows)
        code = sum(r["n_bash_with_exit_code"] for r in rows)
        return _rate(code, bash)
    report["exit_code_coverage"] = {
        "overall": coverage(traj),
        "by_model": {m: coverage([r for r in traj if (r["model"] or r["submission"]) == m])
                     for m in models},
    }

    # 2. census: fraction of trajectories with >=1 finding, per (model, rule_tier)
    census = {}
    for m in models:
        rows = [r for r in traj if (r["model"] or r["submission"]) == m]
        census[m] = {col: _rate(sum(1 for r in rows if r[f"B_{col}"] > 0), len(rows))
                     for col in RULE_TIER_COLS}
    report["census_run_b"] = census

    # 3. before/after (Run A keyless -> Run B contract), summed over all trajectories
    def total(prefix, key):
        return sum(r[f"{prefix}_{key}"] for r in traj)
    report["before_after"] = {
        "suppressed": {"A": total("A", "suppressed"), "B": total("B", "suppressed")},
        "trajectories_with_hard_defect": {
            "A": sum(1 for r in traj if r["A_any_hard_defect"] > 0),
            "B": sum(1 for r in traj if r["B_any_hard_defect"] > 0)},
        "R2a_hard_event": {"A": total("A", "R2a_hard"), "B": total("B", "R2a_hard")},
        "R2a_candidate": {"A": total("A", "R2a_candidate"), "B": total("B", "R2a_candidate")},
        "R1_hard_defect_B": total("B", "R1_hard"), "R2b_hard_defect_B": total("B", "R2b_hard"),
    }

    # 4. outcome association: does the predicate predict an UNRESOLVED run?
    def association(rows):
        n_unres = sum(1 for r in rows if r["resolved"] == 0)
        res = {}
        for name, pred in PREDICATES.items():
            f = [r for r in rows if pred(r)]
            nf = [r for r in rows if not pred(r)]
            f_unres = sum(1 for r in f if r["resolved"] == 0)
            nf_unres = sum(1 for r in nf if r["resolved"] == 0)
            p_f = _rate(f_unres, len(f))  # P(unresolved | F) == precision of F for "will fail"
            p_nf = _rate(nf_unres, len(nf))
            lift = round(p_f["rate"] / p_nf["rate"], 2) if p_nf["rate"] else None
            res[name] = {
                "fired_in": len(f), "p_unresolved_given_F": p_f,
                "p_unresolved_given_notF": p_nf, "lift": lift,
                "recall": _rate(f_unres, n_unres),  # of all unresolved, share F caught
            }
        return res
    report["outcome_association"] = {
        "overall": association(traj),
        "by_model": {m: association([r for r in traj if (r["model"] or r["submission"]) == m])
                     for m in models},
    }

    (out / "aggregates.json").write_text(json.dumps(report, indent=2), encoding="utf-8")

    # 5. audit sample: stratified by (run, rule), up to N each, empty label column
    rng = random.Random(args.seed)
    groups = defaultdict(list)
    for f in findings:
        # Run B for every rule; Run A kept only for R2a (the keyless "before").
        if f["run"] == "B" or (f["run"] == "A" and f["rule"] == "R2a"):
            groups[(f["run"], f["rule"])].append(f)
    audit = []
    for _key, items in sorted(groups.items()):
        rng.shuffle(items)
        audit += items[: args.audit_per_rule]
    cols = ["label", "run", "rule", "tier", "submission", "instance_id", "step", "tool",
            "message", "evidence_excerpt"]
    with (out / "audit_sample.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for row in audit:
            w.writerow({"label": "", **row})

    # console summary
    print(f"=== {len(traj)} trajectories, models: {', '.join(models)} ===")
    cov = report["exit_code_coverage"]["overall"]
    print(f"exit-code coverage: {cov['k']}/{cov['n']} bash results ({cov['rate']:.0%})")
    ba = report["before_after"]
    print(f"before/after  suppressed A {ba['suppressed']['A']} -> B {ba['suppressed']['B']}  |  "
          f"hard_defect trajs A {ba['trajectories_with_hard_defect']['A']} -> "
          f"B {ba['trajectories_with_hard_defect']['B']} (R1 {ba['R1_hard_defect_B']}, "
          f"R2b {ba['R2b_hard_defect_B']})  |  R2a hard A {ba['R2a_hard_event']['A']} -> "
          f"B {ba['R2a_hard_event']['B']}")
    print("outcome association (overall) P(unresolved | finding fired):")
    for name, a in report["outcome_association"]["overall"].items():
        pf, pnf = a["p_unresolved_given_F"], a["p_unresolved_given_notF"]
        print(f"  {name:16} fired {a['fired_in']:4} | P(unres|F) {pf['rate']:.2f} "
              f"{pf['ci95']} vs {pnf['rate']:.2f} | lift {a['lift']}")
    print(f"wrote {out/'aggregates.json'} and {out/'audit_sample.csv'} ({len(audit)} findings)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
