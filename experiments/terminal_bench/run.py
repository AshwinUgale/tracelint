#!/usr/bin/env python3
"""Lint every downloaded Terminal-Bench 2.0 leaderboard trial (Harbor ATIF) with tracelint.

Keyless (no per-harness contract) and zero model calls. Per trial it records how the run ended —
the grader's reward, and ``AgentTimeoutError`` when the harness killed the agent at its time limit —
the findings per rule/tier, and, for each loop (R4), what the agent did after the loop first became
detectable (its ``LOOP_THRESHOLD``-th identical call): agent turns, wall-clock seconds, and tokens
where the trajectory records them. The question this feeds: which harnesses stop a stuck agent, and
how much do the others spend after it is stuck?

Each loop is also tagged so the analysis can separate the kinds a terminal agent produces: a
*wait* (empty keystrokes or ``sleep``, polling a long-running command), and whether the same call
later *advanced* (returned something different — a poll that progressed, not a stuck loop).

    python experiments/terminal_bench/run.py --data <data_dir> --out <out_dir> [--submission S]

Writes ``per_trial.csv``, ``per_loop.csv`` (one row per loop, for the audit) and
``per_finding.csv`` under ``<out_dir>``. Trajectory data is never committed (see ``fetch.py``).
"""

from __future__ import annotations

import argparse
import concurrent.futures
import csv
import gzip
import json
import re
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tracelint.adapters.atif import from_atif_trajectory
from tracelint.findings import LintReport
from tracelint.rules import default_rules, lint_trace
from tracelint.rules.loops import LOOP_THRESHOLD
from tracelint.signatures import call_args_key, looks_empty, result_class, result_fingerprint
from tracelint.tools import ToolRegistry
from tracelint.trace import ResultStatus, ToolCall, Trace

#: (rule, tier) pairs kept as per-trial count columns — every keyless rule that can fire.
RULE_TIERS = [
    ("R2a", "hard_event"), ("R2a", "candidate"), ("R2b", "candidate"), ("R3", "candidate"),
    ("R4", "candidate"), ("R5", "candidate"), ("R6", "hard_defect"), ("R6", "candidate"),
]
TIMEOUT = "AgentTimeoutError"
_WAIT = re.compile(r"^\s*(sleep|wait)\b")
_WAIT_TOOL = re.compile(r"(^|[_.\-])(wait|poll|sleep)([_.\-]|$)", re.IGNORECASE)
_DESCRIPTIVE = frozenset(
    {"summary", "description", "reason", "explanation", "thought", "title", "note", "comment"}
)
_HEX_ID = re.compile(r"\b(?=[0-9a-f]*\d)[0-9a-f]{6,}\b", re.IGNORECASE)  # chunk ids, hashes
_DIGITS = re.compile(r"\d+")
_SPACE = re.compile(r"\s+")


def load_trial(trial_dir: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    gz, plain = trial_dir / "trajectory.json.gz", trial_dir / "trajectory.json"
    raw = gzip.decompress(gz.read_bytes()) if gz.exists() else plain.read_bytes()
    result = json.loads((trial_dir / "result.json").read_text(encoding="utf-8"))
    return json.loads(raw), result


def _ts(value: Any) -> float | None:
    """Epoch seconds from an ISO-8601 timestamp, or ``None``. Harbor writes UTC, but some producers
    drop the offset (``...Z`` / ``+00:00`` in one file, naive in another) — a naive time is UTC, not
    this machine's local time, or the two clocks disagree by hours."""
    if not isinstance(value, str) or not value:
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.timestamp()


def _tokens(step: dict[str, Any]) -> int | None:
    metrics = step.get("metrics") if isinstance(step.get("metrics"), dict) else {}
    parts = [metrics.get(k) for k in ("prompt_tokens", "completion_tokens")]
    ints = [p for p in parts if isinstance(p, int) and not isinstance(p, bool)]
    return sum(ints) if ints else None


Turn = tuple[float | None, int | None]  # (timestamp, tokens) of one agent turn


def agent_turns(doc: dict[str, Any]) -> tuple[dict[str, int], list[Turn]]:
    """``({call_id: turn}, [(timestamp, tokens) per agent turn])`` — the ATIF agent steps the
    adapter turned into calls (copied-context steps are not turns), with the adapter's call ids."""
    call_turn: dict[str, int] = {}
    turns: list[Turn] = []
    for position, step in enumerate(doc.get("steps") or []):
        if not isinstance(step, dict) or str(step.get("source") or "").lower() != "agent":
            continue
        if step.get("is_copied_context") is True:
            continue
        for n, call in enumerate(step.get("tool_calls") or []):
            if isinstance(call, dict):
                call_id = str(call.get("tool_call_id") or f"atif-{position}-{n}")
                call_turn.setdefault(call_id, len(turns))
        turns.append((_ts(step.get("timestamp")), _tokens(step)))
    return call_turn, turns


def _is_wait(call: ToolCall) -> bool:
    """A poll of something running: a tool named for it (``wait_shell_command``), or every input
    argument empty (Terminus's ``keystrokes: ""``, Codex's ``chars: ""``) or a ``sleep`` / ``wait``
    command. Descriptive arguments (a ``summary`` such as "Polling the build") aren't input."""
    if _WAIT_TOOL.search(call.name):
        return True
    texts = [v for k, v in call.args.items()
             if isinstance(v, str) and str(k).lower() not in _DESCRIPTIVE]
    return bool(texts) and all(not t.strip() or _WAIT.match(t) for t in texts)


def _normalized(result: Any) -> str:
    """A result's content with ids, numbers and whitespace runs collapsed, so outputs that differ
    only by a timestamp, pid, counter, or a random chunk id (Codex's polls) compare equal."""
    text = "" if result is None else _excerpt(result.content, 1_000_000)
    return _SPACE.sub(" ", _DIGITS.sub("0", _HEX_ID.sub("#", text))).strip()


def _excerpt(value: Any, n: int) -> str:
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return text.replace("\r", " ").replace("\n", " ")[:n]


def _loops(trace: Trace, report: LintReport, call_turn, turns, finished):
    """One dict per R4 loop, with what happened after it became detectable."""
    by_index = {s.index: s for s in trace.steps}
    calls = trace.tool_calls()
    last_call = calls[-1].index if calls else -1
    total_tokens = sum(t for _, t in turns if t is not None)
    out = []
    for f in report.active_findings:
        if f.finding_type != "loop":
            continue
        idx = list(f.step_indices)
        head, detect = by_index[idx[0]], by_index[idx[LOOP_THRESHOLD - 1]]
        result = trace.result_for(head)
        key, fp = (head.name, call_args_key(head)), result_fingerprint(result)
        later = [c for c in calls if c.index > idx[-1] and (c.name, call_args_key(c)) == key]
        advanced = any(result_fingerprint(trace.result_for(c)) != fp for c in later)
        streak_results = [trace.result_for(by_index[i]) for i in idx]
        recorded = [r for r in streak_results if r is not None]
        turn = call_turn.get(detect.call_id)
        row = {
            "tool": head.name,
            "args": _excerpt(head.args, 120),
            "repeats": len(idx),
            # R4 compares only a coarse result class; these say whether the recorded outputs
            # really were the same (exactly / ignoring numbers, ids, whitespace): ground truth.
            "distinct_results": len({result_fingerprint(r) for r in recorded}),
            "distinct_results_norm": len({_normalized(r) for r in recorded}),
            # What the trace did NOT record: calls with no result (an earlier call in a Terminus
            # batch), empty results, and a call recorded with no arguments (a server-side tool
            # such as OpenAI's web_search). Repeats of unrecorded calls aren't evidence of a loop.
            "missing_results": sum(r is None for r in streak_results),
            "empty_results": sum(r is not None and looks_empty(r.content) for r in streak_results),
            "args_empty": int(not head.args and head.args_unavailable is None),
            "result_class": result_class(result),
            "result_excerpt": _excerpt(result.content if result else "", 160),
            "is_wait": int(_is_wait(head)),
            "advanced_later": int(advanced),
            "runs_to_end": int(idx[-1] == last_call),
            "detect_step": detect.index,
            "detect_turn": turn if turn is not None else "",
            "turns_after": len(turns) - turn - 1 if turn is not None else "",
            "seconds_after": "",
            "tokens_after": "",
        }
        if turn is not None:
            ts = turns[turn][0]
            if ts is not None and finished is not None:
                row["seconds_after"] = round(max(finished - ts, 0.0), 1)
            if total_tokens:
                row["tokens_after"] = sum(t for _, t in turns[turn + 1 :] if t is not None)
        out.append(row)
    return out


def _finding_key(report: LintReport):
    return sorted((f.rule, f.tier.value, tuple(f.step_indices), f.summary)
                  for f in report.active_findings)


def analyze_trial(trial_dir: Path, *, submission: str, check_determinism: bool = False):
    """``(trial_row, loop_rows, finding_rows)`` for one downloaded trial."""
    doc, result = load_trial(trial_dir)
    trace = from_atif_trajectory(doc, run_id=f"{submission}/{trial_dir.name}")
    rules = default_rules()
    report = lint_trace(trace, rules, ToolRegistry())
    deterministic = (not check_determinism) or _finding_key(report) == _finding_key(
        lint_trace(trace, rules, ToolRegistry())
    )

    agent = doc.get("agent") if isinstance(doc.get("agent"), dict) else {}
    config_agent = ((result.get("config") or {}).get("agent")) or {}
    execution = result.get("agent_execution") or {}
    started, finished = _ts(execution.get("started_at")), _ts(execution.get("finished_at"))
    reward = ((result.get("verifier_result") or {}).get("rewards") or {}).get("reward")
    exception = (result.get("exception_info") or {}).get("exception_type") or ""
    call_turn, turns = agent_turns(doc)
    loops = _loops(trace, report, call_turn, turns, finished)
    first = min(loops, key=lambda r: r["detect_step"]) if loops else None
    agent_seconds = round(finished - started, 1) if started and finished else ""

    results = trace.tool_results()
    tiers = Counter((f.rule, f.tier.value) for f in report.active_findings)
    row: dict[str, Any] = {
        "submission": submission,
        "harness": agent.get("name") or config_agent.get("name") or "",
        "model": agent.get("model_name") or config_agent.get("model_name") or "",
        "job": trial_dir.parent.name,
        "trial": trial_dir.name,
        "task": trial_dir.name.rsplit("__", 1)[0],
        "schema": doc.get("schema_version") or "",
        "reward": reward if reward is not None else "",
        "resolved": int(isinstance(reward, (int, float)) and reward >= 1.0),
        "exception": exception,
        "timed_out": int(exception == TIMEOUT),
        "agent_seconds": agent_seconds,
        "n_turns": len(turns),
        "n_calls": len(trace.tool_calls()),
        "n_paired": sum(1 for c in trace.tool_calls() if trace.result_for(c) is not None),
        "n_status_known": sum(r.status is not ResultStatus.UNKNOWN for r in results),
        "has_timestamps": int(bool(turns) and all(ts is not None for ts, _ in turns)),
        "has_tokens": int(any(t is not None for _, t in turns)),
        "n_subagents": len(doc.get("subagent_trajectories") or []),
        "determinism_checked": int(check_determinism),
        "deterministic": int(deterministic),
        "any_hard_defect": int(report.has_hard_defect),
        "n_loops": len(loops),
        "n_wait_loops": sum(r["is_wait"] for r in loops),
        "max_repeats": max((r["repeats"] for r in loops), default=0),
        "loop_at_end": int(any(r["runs_to_end"] for r in loops)),
        "first_loop_turns_after": first["turns_after"] if first else "",
        "first_loop_seconds_after": first["seconds_after"] if first else "",
        "first_loop_tokens_after": first["tokens_after"] if first else "",
        "total_tokens": sum(t for _, t in turns if t is not None) if turns else "",
    }
    for rule, tier in RULE_TIERS:
        row[f"{rule}_{tier}"] = tiers.get((rule, tier), 0)

    ident = {"submission": submission, "trial": trial_dir.name}
    loop_rows = [{**ident, "harness": row["harness"], "model": row["model"],
                  "resolved": row["resolved"], "timed_out": row["timed_out"], **lp} for lp in loops]
    by_index = {s.index: s for s in trace.steps}
    finding_rows = []
    for f in report.active_findings:
        step = f.step_indices[0] if f.step_indices else -1
        s = by_index.get(step)
        finding_rows.append({**ident, "rule": f.rule, "tier": f.tier.value, "step": step,
                             "tool": s.name if isinstance(s, ToolCall) else "",
                             "message": _excerpt(f.summary or "", 160)})
    return row, loop_rows, finding_rows


def trial_dirs(data: Path, submissions: list[str]) -> list[tuple[str, Path]]:
    """``(submission, trial dir)`` for every downloaded trial with a trajectory and a result."""
    subs = submissions or sorted(p.name for p in data.iterdir() if p.is_dir())
    out = []
    for sub in subs:
        for result in sorted((data / sub).glob("*/*/result.json")):
            d = result.parent
            if (d / "trajectory.json.gz").exists() or (d / "trajectory.json").exists():
                out.append((sub, d))
    return out


def _work(item):
    sub, path, determinism = item
    try:
        return analyze_trial(path, submission=sub, check_determinism=determinism)
    except Exception as exc:  # noqa: BLE001 - record and continue; one bad file must not stop a run
        return {"submission": sub, "trial": path.name, "error": repr(exc)[:200]}, [], []


def _write(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    ap.add_argument("--data", required=True, help="directory fetch.py wrote")
    ap.add_argument("--out", required=True)
    ap.add_argument("--submission", action="append", default=[], help="repeatable; default all")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--determinism-every", type=int, default=10,
                    help="re-lint every Nth trial and compare (0 = never)")
    args = ap.parse_args()

    items = [
        (sub, path, bool(args.determinism_every) and i % args.determinism_every == 0)
        for i, (sub, path) in enumerate(trial_dirs(Path(args.data), args.submission))
    ]
    t0 = time.perf_counter()
    trial_rows, loop_rows, finding_rows, errors = [], [], [], []
    with concurrent.futures.ProcessPoolExecutor(max_workers=args.workers) as pool:
        for row, loops, findings in pool.map(_work, items, chunksize=16):
            (errors if "error" in row else trial_rows).append(row)
            loop_rows += loops
            finding_rows += findings
    elapsed = time.perf_counter() - t0

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    _write(out / "per_trial.csv", trial_rows)
    _write(out / "per_loop.csv", loop_rows)
    _write(out / "per_finding.csv", finding_rows)
    _write(out / "errors.csv", errors)

    checked = [r for r in trial_rows if r["determinism_checked"]]
    print(f"=== {len(trial_rows)} trials from {len({r['submission'] for r in trial_rows})} "
          f"submissions in {elapsed:.0f}s, 0 model calls ({len(errors)} unreadable) ===")
    same = sum(r["deterministic"] for r in checked)
    print(f"determinism: {same}/{len(checked)} re-linted trials identical")
    print(f"loops: {sum(r['n_loops'] for r in trial_rows)} in "
          f"{sum(1 for r in trial_rows if r['n_loops'])} trials "
          f"({sum(r['n_wait_loops'] for r in trial_rows)} waits)")
    print(f"timed out: {sum(r['timed_out'] for r in trial_rows)}; resolved: "
          f"{sum(r['resolved'] for r in trial_rows)}")
    print(f"wrote {out}/per_trial.csv, per_loop.csv, per_finding.csv")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
