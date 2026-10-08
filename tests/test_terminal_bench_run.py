"""The Terminal-Bench experiment runner (experiments/terminal_bench/run.py), on synthetic trials
shaped like the leaderboard's: an ATIF trajectory (gzipped) + Harbor's ``result.json``."""

from __future__ import annotations

import gzip
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "experiments"))

from terminal_bench.run import analyze_trial, trial_dirs  # noqa: E402

START = "2026-02-07T04:00:00Z"


def _ts(second: int) -> str:
    return f"2026-02-07T04:{second // 60:02d}:{second % 60:02d}Z"


def _turn(step_id, call_id, args, output, *, second, tokens=None, name="bash"):
    step = {
        "step_id": step_id, "source": "agent", "message": "", "timestamp": _ts(second),
        "tool_calls": [{"tool_call_id": call_id, "function_name": name, "arguments": args}],
        "observation": {"results": [{"source_call_id": call_id, "content": output}]},
    }
    if tokens is not None:
        step["metrics"] = {"prompt_tokens": tokens, "completion_tokens": 0}
    return step


def _write_trial(root, turns, *, reward=0.0, exception=None, finished=600, sub="H__M"):
    trial = root / sub / "job-1" / "task-a__X1"
    trial.mkdir(parents=True)
    doc = {
        "schema_version": "ATIF-v1.6", "session_id": "s",
        "agent": {"name": "harness", "version": "1", "model_name": "model-x"},
        "steps": [{"step_id": 1, "source": "user", "message": "build it"}, *turns],
    }
    (trial / "trajectory.json.gz").write_bytes(gzip.compress(json.dumps(doc).encode()))
    result = {
        "verifier_result": {"rewards": {"reward": reward}},
        "exception_info": {"exception_type": exception} if exception else None,
        "agent_execution": {"started_at": START, "finished_at": _ts(finished)},
    }
    (trial / "result.json").write_text(json.dumps(result), encoding="utf-8")
    return trial


def test_a_stuck_loop_to_the_timeout_is_measured_from_its_detection(tmp_path):
    make = {"command": "make"}
    turns = [
        _turn(2, "c1", {"command": "ls"}, "src", second=10, tokens=100),
        *[_turn(3 + i, f"m{i}", make, "make: *** No targets.", second=20 + 10 * i, tokens=100)
          for i in range(5)],
    ]
    trial = _write_trial(tmp_path, turns, exception="AgentTimeoutError", finished=600)
    row, loops, _ = analyze_trial(trial, submission="H__M")

    assert (row["timed_out"], row["resolved"], row["harness"], row["task"]) == (
        1, 0, "harness", "task-a"
    )
    assert (row["n_loops"], row["max_repeats"], row["loop_at_end"]) == (1, 5, 1)
    (loop,) = loops
    # detectable at the 3rd identical call (turn 3 of 5 make turns, second 40): 2 turns after
    assert loop["turns_after"] == 2
    assert loop["seconds_after"] == 600 - 40
    assert loop["tokens_after"] == 200
    assert (loop["distinct_results"], loop["distinct_results_norm"]) == (1, 1)
    assert (loop["is_wait"], loop["runs_to_end"], loop["advanced_later"]) == (0, 1, 0)
    assert row["n_identical_loops"] == 1


def test_a_wait_whose_screen_changes_is_told_apart(tmp_path):
    wait = {"keystrokes": "", "duration": 30}
    screens = ["Building... 10%", "Building... 40%", "Building... 90%"]
    turns = [_turn(2 + i, f"w{i}", wait, s, second=10 * i, name="bash_command")
             for i, s in enumerate(screens)]
    turns.append(_turn(5, "d", {"keystrokes": "ls\n", "duration": 1}, "out", second=40,
                       name="bash_command"))
    row, loops, _ = analyze_trial(_write_trial(tmp_path, turns, reward=1.0), submission="H__M")

    assert row["resolved"] == 1 and row["timed_out"] == 0
    (loop,) = loops  # R4 compares a coarse result class, so it groups these three waits
    assert loop["is_wait"] == 1
    assert loop["distinct_results"] == 3  # ...but the output changed every time
    assert loop["distinct_results_norm"] == 1  # only the numbers changed
    assert loop["runs_to_end"] == 0 and row["n_wait_loops"] == 1


def test_a_poll_that_differs_only_by_its_chunk_id_is_not_progress(tmp_path):
    # Codex-style polls of a running process: a fresh random chunk id and wall time each time,
    # no new output — the same state, so not "changing".
    poll = {"session_id": 7, "chars": ""}
    outputs = [f"Chunk ID: {cid}\nWall time: 5.00{i} seconds\nProcess running\nOutput:\n"
               for i, cid in enumerate(["23913d", "0b182f", "d2be97"])]
    turns = [_turn(2 + i, f"p{i}", poll, out, second=5 * i, name="write_stdin")
             for i, out in enumerate(outputs)]
    _, loops, _ = analyze_trial(_write_trial(tmp_path, turns), submission="H__M")
    (loop,) = loops
    assert loop["is_wait"] == 1
    assert (loop["distinct_results"], loop["distinct_results_norm"]) == (3, 1)


def test_trial_dirs_finds_complete_trials_only(tmp_path):
    _write_trial(tmp_path, [_turn(2, "c", {"command": "ls"}, "x", second=1)])
    (tmp_path / "H__M" / "job-1" / "partial__Y").mkdir()
    assert [d.name for _, d in trial_dirs(tmp_path, [])] == ["task-a__X1"]
