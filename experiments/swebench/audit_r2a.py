#!/usr/bin/env python3
"""Audit R2a (tool error) on the SWE-bench study's findings — by reading what happened, not the
exit code.

The first audit labeled every exit-code finding (``hard_event``, exit != 0) a true error *by
definition*, so its "100% precise" was circular. A non-zero exit is not always an error: ``grep``
with no match exits 1, ``diff`` exits 1 when files differ, a test run exits 1 when tests fail. This
audit draws a seeded, model-stratified sample of R2a findings, shows each one's command and output,
and takes a label from a reader who judges the output itself:

- ``error``             the command failed at its job: not found, syntax / usage error, an
  unintended crash, permission denied, a file it needed missing, a timeout or kill;
- ``expected_failure``  the command worked and reported a failure the agent was looking for: a
  test run with failing tests, a reproduction script raising the bug;
- ``informational``     non-zero (or an error-like word) by convention, nothing failed: ``grep``
  with no match, ``diff`` finding differences, a false ``test`` check, a file's own text;
- ``unclear``           the trace doesn't show enough to tell.

Precision is reported strictly (``error``) and broadly (``error`` + ``expected_failure``), for the
exit-code findings and the string-match findings (``candidate``) under the same criterion.

    python audit_r2a.py sample --data <swebench_data> --runs <runs_dir> --out sample.csv
    python audit_r2a.py summarize --labels labeled.csv

The committed ``r2a_audit_labels.csv`` holds only ids and labels (no trajectory text).
"""

from __future__ import annotations

import argparse
import csv
import random
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from swebench.adapter import extract_exit_code, load_trajectory  # noqa: E402
from tracelint.stats import wilson_interval  # noqa: E402
from tracelint.trace import ToolCall, ToolResult  # noqa: E402

LABELS = ("error", "expected_failure", "informational", "unclear")
KEY = ("submission", "instance_id", "step")


def _excerpt(text: str, head: int = 220, tail: int = 320) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= head + tail + 5 else f"{text[:head]} … {text[-tail:]}"


def sample(data: Path, runs: Path, per_cell: dict[str, int], seed: int) -> list[dict]:
    """A seeded sample of Run-B R2a findings, ``per_cell[tier]`` per (submission, tier)."""
    cells: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for path in sorted(runs.glob("*/per_finding.csv")):
        with path.open(encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                if row["run"] == "B" and row["rule"] == "R2a" and row["tier"] in per_cell:
                    cells[(row["submission"], row["tier"])].append(row)
    rng = random.Random(seed)
    out = []
    for (sub, tier), rows in sorted(cells.items()):
        for row in rng.sample(rows, min(per_cell[tier], len(rows))):
            trace = load_trajectory(
                str(data / sub / "trajs" / f"{row['instance_id']}.json"),
                submission=sub, instance_id=row["instance_id"],
            )
            step = trace.steps[int(row["step"])]
            if isinstance(step, ToolResult):
                result, call = step, trace.call_for(step)
            else:
                call = step if isinstance(step, ToolCall) else None
                result = trace.result_for(call) if call else None
            content = str(result.content) if result else ""
            command = (call.args.get("command") or call.args.get("path") or "") if call else ""
            out.append({
                "submission": sub, "instance_id": row["instance_id"], "step": row["step"],
                "tier": tier, "tool": call.name if call else "",
                "exit_code": extract_exit_code(content),
                "command": _excerpt(command, 160, 60),
                "output": _excerpt(content),
                "label": "", "note": "",
            })
    return out


def summarize(rows: list[dict]) -> dict[str, dict]:
    """Strict and broad precision per tier, overall and per submission, with Wilson 95% CIs."""
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in rows:
        if row["label"] not in LABELS:
            raise ValueError(f"unlabeled or unknown label {row['label']!r} for {row}")
        groups[(row["tier"], "all")].append(row)
        groups[(row["tier"], row["submission"])].append(row)
    out = {}
    for (tier, sub), items in sorted(groups.items()):
        judged = [r for r in items if r["label"] != "unclear"]
        n = len(judged)
        strict = sum(r["label"] == "error" for r in judged)
        broad = sum(r["label"] in ("error", "expected_failure") for r in judged)
        out[f"{tier}/{sub}"] = {
            "n": n,
            "unclear": len(items) - n,
            "by_label": {lab: sum(r["label"] == lab for r in items) for lab in LABELS},
            "strict": _rate(strict, n),
            "broad": _rate(broad, n),
        }
    return out


def _rate(k: int, n: int) -> dict:
    lo, hi = wilson_interval(k, n) if n else (0.0, 0.0)
    return {"k": k, "p": round(k / n, 3) if n else None, "ci": [round(lo, 3), round(hi, 3)]}


def _write(path: Path, rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sample")
    s.add_argument("--data", required=True)
    s.add_argument("--runs", required=True)
    s.add_argument("--out", required=True)
    s.add_argument("--hard", type=int, default=30, help="exit-code findings per submission")
    s.add_argument("--candidate", type=int, default=15, help="string-match findings per submission")
    s.add_argument("--seed", type=int, default=20261008)
    m = sub.add_parser("summarize")
    m.add_argument("--labels", required=True)
    args = ap.parse_args()

    if args.cmd == "sample":
        per_cell = {"hard_event": args.hard, "candidate": args.candidate}
        rows = sample(Path(args.data), Path(args.runs), per_cell, args.seed)
        _write(Path(args.out), rows)
        print(f"wrote {len(rows)} findings to label -> {args.out}")
        return 0
    with open(args.labels, encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    for key, s in summarize(rows).items():
        print(f"{key:48} n={s['n']:>3} (+{s['unclear']} unclear)  "
              f"strict {s['strict']['p']} {s['strict']['ci']}  broad {s['broad']['p']} "
              f"{s['broad']['ci']}  {s['by_label']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
