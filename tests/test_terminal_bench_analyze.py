"""The Terminal-Bench experiment's analysis (experiments/terminal_bench/analyze.py), on synthetic
runner output: loop classification by ground truth, per-harness loop handling, and outcome."""

from __future__ import annotations

import csv
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "experiments"))

from terminal_bench.analyze import analyze, loop_kind  # noqa: E402


@pytest.mark.parametrize(
    ("norm", "wait", "advanced", "unrecorded", "kind"),
    [
        (1, 0, 0, {}, "stuck"),  # same action, same output
        (3, 0, 0, {}, "changing"),  # R4's coarse class grouped outputs that differed
        (2, 1, 1, {}, "changing"),  # a wait whose screen changed is progress, not a loop
        (1, 1, 0, {}, "wait-silent"),
        (1, 1, 1, {}, "wait-advanced"),  # the same poll later returned something else
        # an earlier call in a Terminus batch has no result of its own
        (1, 0, 0, {"missing_results": 2}, "unrecorded"),
        # a server-side tool recorded with no arguments and empty results
        (1, 0, 0, {"args_empty": 1, "empty_results": 3}, "unrecorded"),
        # no arguments but real, identical output: that is a stuck repeat
        (1, 0, 0, {"args_empty": 1, "empty_results": 0}, "stuck"),
    ],
)
def test_loop_kind(norm, wait, advanced, unrecorded, kind):
    row = {"repeats": 3, "distinct_results_norm": norm, "is_wait": wait,
           "advanced_later": advanced, **unrecorded}
    assert loop_kind(row) == kind


def _trial(sub, trial, resolved, *, seconds=100, timed_out=0):
    return {"submission": sub, "harness": sub.split("__")[0], "trial": trial,
            "resolved": resolved, "timed_out": timed_out, "agent_seconds": seconds, "n_calls": 5,
            "determinism_checked": 1, "deterministic": 1}


def _loop(sub, trial, *, repeats=3, norm=1, wait=0, to_end=0, timed_out=0, after=(5, 50.0)):
    return {"submission": sub, "trial": trial, "repeats": repeats, "distinct_results_norm": norm,
            "is_wait": wait, "advanced_later": 0, "runs_to_end": to_end, "timed_out": timed_out,
            "detect_step": 10, "turns_after": after[0], "seconds_after": after[1]}


def _write(path, rows):
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def test_per_harness_loop_handling_and_outcome(tmp_path):
    # Harness A has a breaker (short loops); harness B lets a stuck agent run to the time limit.
    trials = [
        _trial("A__m", "t1", 0), _trial("A__m", "t2", 1), _trial("A__m", "t3", 1),
        _trial("B__m", "t1", 0, seconds=900, timed_out=1),
        _trial("B__m", "t2", 0, seconds=900, timed_out=1),
        _trial("B__m", "t3", 1),
    ]
    loops = [
        _loop("A__m", "t1", repeats=3),
        _loop("A__m", "t2", norm=4),  # changing output: not stuck
        _loop("B__m", "t1", repeats=200, to_end=1, timed_out=1, after=(197, 800.0)),
        _loop("B__m", "t2", repeats=60, to_end=1, timed_out=1, after=(57, 450.0)),
    ]
    _write(tmp_path / "per_trial.csv", trials)
    _write(tmp_path / "per_loop.csv", loops)
    agg, sample = analyze(tmp_path, per_cell=1)

    assert agg["corpus"]["trials"] == 6 and agg["corpus"]["determinism"] == "6/6"
    assert agg["overall"]["loops_by_kind"] == {
        "stuck": 3, "wait-silent": 0, "wait-advanced": 0, "changing": 1, "unrecorded": 0
    }
    a, b = agg["by_submission"]["A__m"], agg["by_submission"]["B__m"]
    assert a["stuck_repeats"]["max"] == 3 and b["stuck_repeats"]["max"] == 200
    assert b["stuck_loops_running_at_end"]["p"] == 1.0 and b["of_those_timed_out"]["p"] == 1.0
    # every stuck trial failed; base failure rate over all six trials is 3/6
    over = agg["overall"]
    assert over["fail_given_stuck"]["p"] == 1.0 and over["fail_base"] == 0.5
    assert over["lift"] == 2.0
    after = b["after_detection_failed_runs"]
    assert after["median_turns"] == 127.0 and after["median_seconds"] == 625.0
    assert after["share_of_all_failed_run_seconds"] == round((800 + 450) / 1800, 3)
    # one loop per (submission, kind) cell, each with an empty label to fill in
    assert len(sample) == 3 and all(row["label"] == "" for row in sample)
