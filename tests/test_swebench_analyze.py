"""The SWE-bench experiment analysis step (experiments/swebench/analyze.py), end to end."""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "experiments"))

from swebench import analyze  # noqa: E402

_EXTRAS = ["exit_code", "suppressed", "any_hard_defect", "any_hard_event", "any_candidate"]
_COLS = (
    ["submission", "model", "instance_id", "resolved", "n_steps", "n_tool_calls",
     "n_bash", "n_bash_with_exit_code", "n_bash_exit_nonzero"]
    + [f"{p}_{c}" for p in ("A", "B") for c in analyze.RULE_TIER_COLS + _EXTRAS]
)


def _row(**over):
    row = {c: 0 for c in _COLS}
    row.update(submission="sub", model="m1", instance_id="x", n_bash=2, n_bash_with_exit_code=1)
    row.update(over)
    return row


def _write(runs: Path):
    (runs / "s1").mkdir(parents=True)
    with (runs / "s1" / "per_trajectory.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=_COLS)
        w.writeheader()
        w.writerow(_row(instance_id="ok", resolved=1, n_bash_with_exit_code=2))
        w.writerow(_row(instance_id="bad", resolved=0, B_R1_hard=1, B_any_hard_defect=1))
    with (runs / "s1" / "per_finding.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["submission", "instance_id", "run", "rule", "tier",
                                           "finding_type", "step", "tool", "message",
                                           "evidence_excerpt"])
        w.writeheader()
        w.writerow({"submission": "sub", "instance_id": "bad", "run": "B", "rule": "R1",
                    "tier": "hard_defect", "finding_type": "schema_violation", "step": 3,
                    "tool": "str_replace_editor.create", "message": "schema",
                    "evidence_excerpt": "x"})
        w.writerow({"submission": "sub", "instance_id": "ok", "run": "A", "rule": "R2a",
                    "tier": "candidate", "finding_type": "tool_error_event", "step": 1,
                    "tool": "execute_bash", "message": "maybe error", "evidence_excerpt": "y"})


def test_analyze_emits_aggregates_and_audit(tmp_path, monkeypatch):
    runs, out = tmp_path / "runs", tmp_path / "out"
    _write(runs)
    monkeypatch.setattr(sys, "argv", ["analyze.py", "--runs", str(runs), "--out", str(out)])
    assert analyze.main() == 0

    report = json.loads((out / "aggregates.json").read_text(encoding="utf-8"))
    assert report["n_trajectories"] == 2
    # exit-code coverage: 2 + 1 of 2 + 2 bash results = 3/4.
    assert report["exit_code_coverage"]["overall"]["k"] == 3
    assert report["exit_code_coverage"]["overall"]["n"] == 4
    # one trajectory has a hard defect under the contract (Run B).
    assert report["before_after"]["trajectories_with_hard_defect"]["B"] == 1
    # the hard-defect run is the unresolved one -> P(unresolved | any_hard_defect) == 1.0.
    assoc = report["outcome_association"]["overall"]["any_hard_defect"]
    assert assoc["p_unresolved_given_F"]["rate"] == 1.0

    audit = list(csv.DictReader((out / "audit_sample.csv").open(encoding="utf-8")))
    assert {r["rule"] for r in audit} == {"R1", "R2a"}
    assert all(r["label"] == "" for r in audit)  # unlabeled, for the hand audit
