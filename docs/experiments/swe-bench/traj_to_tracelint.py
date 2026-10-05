#!/usr/bin/env python3
"""Convert a SWE-agent `.traj` trajectory into a native tracelint Trace (precision experiment).

Each (action, observation) turn becomes a ToolCall + ToolResult: the tool name is the action's
first shell token, args = {"command": <full action>} (so identical commands read as identical
calls, which is what R4/R5 key on), result content = <observation>, status = UNKNOWN. We
deliberately do NOT pre-label any result as an error — the whole point is to see whether tracelint's
own heuristics over-fire on legitimate bash output and retries. The problem statement is seeded as a
user Message so R3's provenance isn't starved of the context the agent actually had.

No tools.json is used, so the schema/side-effect rules (R1, R2b, R8-R12) stay dormant; this
exercises the keyless, retry-sensitive rules (R2a tool-error, R3 hallucinated-arg, R4 loop, R5
redundant). It is a PRECISION / over-fire probe: the trajectories are unlabeled, so a firing is a
candidate false positive to inspect, not a confirmed miss or hit.

Usage: python traj_to_tracelint.py <in.traj> [out.native.json]
"""

from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path

from tracelint.trace import Message, ResultStatus, Role, ToolCall, ToolResult, Trace


def tool_name(action: str) -> str:
    """The tool for a step: the action command's first shell token (best-effort)."""
    action = (action or "").strip()
    if not action:
        return "noop"
    try:
        tokens = shlex.split(action)
    except ValueError:  # unbalanced quotes etc. — fall back to a plain split
        tokens = action.split()
    return tokens[0] if tokens else "command"


def problem_statement(data: dict) -> str | None:
    """Best-effort extraction of the task text, across .traj versions."""
    info = data.get("info") or {}
    for key in ("problem_statement", "issue", "task"):
        value = info.get(key)
        if isinstance(value, str) and value.strip():
            return value
    traj = data.get("trajectory") or []
    if traj and isinstance(traj[0], dict):
        query = traj[0].get("query")
        if isinstance(query, list):
            for msg in query:
                if isinstance(msg, dict) and msg.get("role") == "user" and msg.get("content"):
                    content = msg["content"]
                    return content if isinstance(content, str) else json.dumps(content)
    return None


def convert(data: dict, *, run_id: str) -> Trace:
    steps: list = []
    task = problem_statement(data)
    if task:
        steps.append(Message(role=Role.USER, content=task[:4000]))
    traj = data.get("trajectory")
    if not isinstance(traj, list):
        raise SystemExit("unsupported .traj: no top-level 'trajectory' list")
    index = 0
    for turn in traj:
        if not isinstance(turn, dict):
            continue
        action = turn.get("action")
        if not isinstance(action, str) or not action.strip():
            continue
        call_id = f"c{index}"
        steps.append(ToolCall(call_id=call_id, name=tool_name(action), args={"command": action.strip()}))
        obs = turn.get("observation")
        content = obs if isinstance(obs, str) else ("" if obs is None else json.dumps(obs))
        steps.append(ToolResult(call_id=call_id, content=content, status=ResultStatus.UNKNOWN))
        index += 1
    return Trace(run_id=run_id, steps=steps)


def main(argv: list[str]) -> int:
    src = Path(argv[1])
    out = Path(argv[2]) if len(argv) > 2 else src.with_suffix(".native.json")
    data = json.loads(src.read_text(encoding="utf-8"))
    run_id = str((data.get("info") or {}).get("instance_id") or src.stem)
    trace = convert(data, run_id=run_id)
    out.write_text(trace.to_json(), encoding="utf-8")
    n_calls = sum(1 for s in trace.steps if type(s).__name__ == "ToolCall")
    print(f"{src.name}: {n_calls} calls -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
