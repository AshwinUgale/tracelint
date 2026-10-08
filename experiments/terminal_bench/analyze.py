#!/usr/bin/env python3
"""Aggregate the Terminal-Bench runner's CSVs: loop handling per harness, outcome, and an audit
sample.

Every loop (R4) is first classified by what its repeats actually returned — the ground truth R4
cannot see, since it compares only a coarse result class:

- ``stuck``    the same action, the same output (ignoring numbers and whitespace): no progress;
- ``wait``     a poll (empty keystrokes / ``sleep``) whose output did not change — ``advanced`` if
  the same poll later returned something different (a long command that finished), else ``silent``;
- ``changing`` the output differed between repeats: progress R4's coarse class missed;
- ``unrecorded`` the trace doesn't show what the repeats returned — a call with no result (an
  earlier call in a Terminus batch), or a call recorded with no arguments and an empty result (a
  server-side tool such as OpenAI's ``web_search``): not evidence either way.

Then, per harness (each submission is one harness + model), it reports how long stuck loops ran,
how often the run ended inside one (and whether by the harness's time limit), what was spent after
the loop became detectable, and whether a stuck loop goes with the run failing (Wilson 95%
intervals). Plus a stratified ``audit_sample.csv`` to check the classification by hand.

    python experiments/terminal_bench/analyze.py --runs <run.py out dir> --out <dir>
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any

from tracelint.stats import wilson_interval

KINDS = ("stuck", "wait-silent", "wait-advanced", "changing", "unrecorded")


def loop_kind(loop: dict[str, Any]) -> str:
    """Classify one ``per_loop.csv`` row by what its repeats returned (module docstring)."""
    repeats = int(loop["repeats"])
    missing = int(loop.get("missing_results") or 0)
    empty = int(loop.get("empty_results") or 0)
    if missing or (int(loop.get("args_empty") or 0) and empty == repeats):
        return "unrecorded"
    if int(loop["distinct_results_norm"]) > 1:
        return "changing"
    if int(loop["is_wait"]):
        return "wait-advanced" if int(loop["advanced_later"]) else "wait-silent"
    return "stuck"


def _num(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _rate(k: int, n: int) -> dict[str, Any]:
    lo, hi = wilson_interval(k, n) if n else (0.0, 0.0)
    return {"k": k, "n": n, "p": round(k / n, 3) if n else None, "ci": [round(lo, 3), round(hi, 3)]}


def _median(values: list[float]) -> float | None:
    return round(statistics.median(values), 1) if values else None


def _quantile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(q * len(ordered)))]


def first_stuck(loops: list[dict[str, Any]]) -> dict[str, Any] | None:
    """A trial's earliest-detected stuck loop."""
    stuck = [lp for lp in loops if lp["kind"] == "stuck"]
    return min(stuck, key=lambda lp: int(lp["detect_step"])) if stuck else None


def summarize(trials: list[dict[str, Any]], loops_by_trial: dict[tuple, list]) -> dict[str, Any]:
    """Loop-handling and outcome numbers for one group of trials."""
    n = len(trials)
    failed = [t for t in trials if not int(t["resolved"])]
    has_stuck = [first_stuck(loops_by_trial[(t["submission"], t["trial"])]) is not None
                 for t in trials]
    with_stuck = [t for t, s in zip(trials, has_stuck, strict=True) if s]
    without = [t for t, s in zip(trials, has_stuck, strict=True) if not s]
    loops = [lp for t in trials for lp in loops_by_trial[(t["submission"], t["trial"])]]
    stuck = [lp for lp in loops if lp["kind"] == "stuck"]
    repeats = [int(lp["repeats"]) for lp in stuck]

    # After the first stuck loop was detectable, in runs that failed anyway.
    after_turns, after_seconds, after_share = [], [], []
    spent_after = spent_total = 0.0
    for t in failed:
        lp = first_stuck(loops_by_trial[(t["submission"], t["trial"])])
        total = _num(t["agent_seconds"])
        if total:
            spent_total += total
        if lp is None:
            continue
        if _num(lp["turns_after"]) is not None:
            after_turns.append(_num(lp["turns_after"]))
        sec = _num(lp["seconds_after"])
        if sec is not None and total:
            after_seconds.append(sec)
            after_share.append(min(sec / total, 1.0))
            spent_after += min(sec, total)

    fail_stuck = sum(1 for t in with_stuck if not int(t["resolved"]))
    fail_other = sum(1 for t in without if not int(t["resolved"]))
    base = len(failed) / n if n else None
    p_stuck = fail_stuck / len(with_stuck) if with_stuck else None
    ends_in_stuck = [lp for lp in stuck if int(lp["runs_to_end"])]
    return {
        "trials": n,
        "resolved": _rate(n - len(failed), n),
        "timed_out": _rate(sum(int(t["timed_out"]) for t in trials), n),
        "loops_by_kind": {k: sum(lp["kind"] == k for lp in loops) for k in KINDS},
        "trials_with_stuck_loop": _rate(len(with_stuck), n),
        "stuck_repeats": {
            "median": _median(repeats), "p90": _quantile(repeats, 0.9),
            "max": max(repeats, default=None),
        },
        "stuck_loops_running_at_end": _rate(len(ends_in_stuck), len(stuck)),
        "of_those_timed_out": _rate(sum(int(lp["timed_out"]) for lp in ends_in_stuck),
                                    len(ends_in_stuck)),
        "fail_given_stuck": _rate(fail_stuck, len(with_stuck)),
        "fail_given_no_stuck": _rate(fail_other, len(without)),
        "fail_base": round(base, 3) if base is not None else None,
        "lift": round(p_stuck / base, 2) if p_stuck is not None and base else None,
        "after_detection_failed_runs": {
            "runs": len(after_turns),
            "median_turns": _median(after_turns),
            "median_seconds": _median(after_seconds),
            "median_share_of_run": _median(after_share),
            "share_of_all_failed_run_seconds": (
                round(spent_after / spent_total, 3) if spent_total else None
            ),
        },
    }


def audit_sample(loops: list[dict[str, Any]], per_cell: int, seed: int) -> list[dict[str, Any]]:
    """Up to ``per_cell`` loops per (harness submission, kind), with an empty ``label`` column."""
    rng = random.Random(seed)
    cells: dict[tuple, list] = defaultdict(list)
    for lp in loops:
        cells[(lp["submission"], lp["kind"])].append(lp)
    out = []
    for key in sorted(cells):
        rows = cells[key]
        for lp in rng.sample(rows, min(per_cell, len(rows))):
            out.append({"label": "", **lp})
    return out


def _read(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def analyze(runs: Path, *, per_cell: int = 5, seed: int = 0) -> tuple[dict, list[dict]]:
    trials = _read(runs / "per_trial.csv")
    loops = _read(runs / "per_loop.csv")
    for lp in loops:
        lp["kind"] = loop_kind(lp)
    loops_by_trial: dict[tuple, list] = defaultdict(list)
    for lp in loops:
        loops_by_trial[(lp["submission"], lp["trial"])].append(lp)

    by_submission: dict[str, list] = defaultdict(list)
    by_harness: dict[str, list] = defaultdict(list)
    for t in trials:
        by_submission[t["submission"]].append(t)
        by_harness[t["harness"]].append(t)

    checked = [t for t in trials if int(t.get("determinism_checked") or 0)]
    aggregates = {
        "corpus": {
            "trials": len(trials),
            "submissions": len(by_submission),
            "harnesses": sorted(by_harness),
            "trials_with_tool_calls": sum(1 for t in trials if int(t["n_calls"])),
            "determinism": f"{sum(int(t['deterministic']) for t in checked)}/{len(checked)}",
            "loops": len(loops),
        },
        "overall": summarize(trials, loops_by_trial),
        "by_submission": {
            s: summarize(ts, loops_by_trial) for s, ts in sorted(by_submission.items())
        },
        "by_harness": {h: summarize(ts, loops_by_trial) for h, ts in sorted(by_harness.items())},
    }
    return aggregates, audit_sample(loops, per_cell, seed)


def _cell(value: Any) -> str:
    if value is None or value == "":
        return "-"
    return f"{value:.2f}" if isinstance(value, float) else str(value)


def _row(name: str, s: dict[str, Any]) -> list[str]:
    r, a = s["stuck_repeats"], s["after_detection_failed_runs"]
    return [
        name[:44], str(s["trials"]), _cell(s["trials_with_stuck_loop"]["p"]),
        _cell(r["median"]), _cell(r["p90"]), _cell(r["max"]),
        _cell(s["stuck_loops_running_at_end"]["p"]), _cell(s["of_those_timed_out"]["p"]),
        _cell(s["fail_given_stuck"]["p"]), _cell(s["lift"]),
        _cell(a["median_turns"]), _cell(a["median_seconds"]),
        _cell(a["share_of_all_failed_run_seconds"]),
    ]


def _print(agg: dict[str, Any]) -> None:
    c, o = agg["corpus"], agg["overall"]
    print(f"{c['trials']} trials, {c['submissions']} submissions, {len(c['harnesses'])} harnesses; "
          f"determinism {c['determinism']}; {c['loops']} loops {o['loops_by_kind']}")
    s = o["fail_given_stuck"]
    print(f"overall: fail base {o['fail_base']}, P(fail|stuck) {s['p']} {s['ci']} n={s['n']}, "
          f"lift {o['lift']}")
    widths = [44, 6, 6, 5, 5, 5, 5, 5, 6, 5, 6, 7, 6]
    head = ["submission", "trials", "stuck", "med", "p90", "max", "@end", "TO", "P(f|s)", "lift",
            "t_aft", "s_aft", "share"]
    def line(cells: list[str]) -> str:
        pairs = enumerate(zip(cells, widths, strict=True))
        return " ".join(v.rjust(w) if i else v.ljust(w) for i, (v, w) in pairs)

    print(line(head))
    for name, summary in agg["by_submission"].items():
        print(line(_row(name, summary)))


def main() -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    ap.add_argument("--runs", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--per-cell", type=int, default=5, help="audit loops per (submission, kind)")
    args = ap.parse_args()
    agg, sample = analyze(Path(args.runs), per_cell=args.per_cell)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "aggregates.json").write_text(json.dumps(agg, indent=1), encoding="utf-8")
    if sample:
        with (out / "audit_sample.csv").open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(sample[0]))
            writer.writeheader()
            writer.writerows(sample)
    _print(agg)
    print(f"wrote {out/'aggregates.json'} and {out/'audit_sample.csv'} ({len(sample)} loops)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
