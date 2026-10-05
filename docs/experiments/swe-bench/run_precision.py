#!/usr/bin/env python3
"""SWE-bench precision / over-fire experiment.

Lint a sample of REAL, verified-correct mini-swe-agent trajectories (one row = one run, from the
nanoswe/nanoswe-trajs HF dataset) and measure how often tracelint's keyless rules fire. Because every
sampled run is `verified=True` (its patch resolved the issue) and `Submitted`, a firing is a
candidate FALSE POSITIVE to inspect — this is a precision probe, not a recall test.

Mapping (faithful): each assistant tool call `{"name": "bash", "arguments": {"command": ...}}`
becomes a ToolCall(name, args); the following user message (the `<returncode>…<output>…`) becomes its
ToolResult, left status=UNKNOWN so we never pre-label an error — tracelint's own heuristics decide.
The first user message (the issue) is seeded as context so R3 provenance isn't starved. No tools.json,
so R1/R7 suppress and the side-effect rules stay dormant; this exercises R2a/R3/R4/R5.
"""

from __future__ import annotations

import json
import random
import sys
from collections import Counter
from pathlib import Path

import pandas as pd

from tracelint.findings import ConfidenceTier
from tracelint.rules import default_rules, lint_trace
from tracelint.tools import ToolRegistry
from tracelint.trace import Message, ResultStatus, Role, ToolCall, ToolResult, Trace


def extract_action(parts) -> tuple[str, dict] | None:
    """Pull (tool_name, args) from an assistant message's `parts` (a JSON tool-call part)."""
    for p in parts if parts is not None else []:
        if not isinstance(p, dict) or p.get("type") == "text":
            continue
        text = p.get("text")
        if not isinstance(text, str):
            continue
        try:
            obj = json.loads(text)
        except (ValueError, TypeError):
            continue
        if isinstance(obj, dict) and ("name" in obj or "arguments" in obj or "command" in obj):
            name = str(obj.get("name") or "tool")
            args = obj.get("arguments")
            if not isinstance(args, dict):
                args = {"command": obj["command"]} if "command" in obj else {"input": args}
            return name, args
    return None


def messages_to_trace(msgs, run_id: str) -> Trace:
    steps: list = []
    if len(msgs) and msgs[0].get("role") == "user" and msgs[0].get("content"):
        steps.append(Message(role=Role.USER, content=str(msgs[0]["content"])[:4000]))
    cid = 0
    for idx in range(len(msgs)):
        m = msgs[idx]
        if m.get("role") != "assistant":
            continue
        action = extract_action(m.get("parts"))
        if action is None:
            continue
        name, args = action
        call_id = f"c{cid}"
        cid += 1
        steps.append(ToolCall(call_id=call_id, name=name, args=args))
        obs = ""
        if idx + 1 < len(msgs) and msgs[idx + 1].get("role") == "user":
            obs = str(msgs[idx + 1].get("content") or "")
        steps.append(ToolResult(call_id=call_id, content=obs, status=ResultStatus.UNKNOWN))
    return Trace(run_id=run_id, steps=steps)


def main() -> int:
    shard = Path(sys.argv[1])
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 50
    df = pd.read_parquet(shard)
    pool = df[(df["verified"]) & (df["exit_status"].str.lower() == "submitted")]
    idx = random.Random(20261005).sample(range(len(pool)), min(n, len(pool)))
    rows = pool.iloc[idx]

    rules = default_rules()
    registry = ToolRegistry()
    n_traj = n_calls = 0
    exit_codes: Counter = Counter()
    rule_tier_findings: Counter = Counter()  # (rule, tier) -> total findings
    rule_traj: Counter = Counter()  # rule -> # trajectories firing it at least once
    traj_with_defect = traj_with_event = 0
    traj_with_repeat2 = traj_with_repeat3 = 0  # retries present: an identical call seen >=2 / >=3x
    examples: dict[str, str] = {}

    for _, row in rows.iterrows():
        trace = messages_to_trace(row["messages"], str(row["instance_id"]))
        calls = sum(1 for s in trace.steps if isinstance(s, ToolCall))
        if calls == 0:
            continue
        n_traj += 1
        n_calls += calls
        sigs = Counter(
            (s.name, json.dumps(s.args, sort_keys=True))
            for s in trace.steps
            if isinstance(s, ToolCall)
        )
        top = max(sigs.values())
        traj_with_repeat2 += top >= 2
        traj_with_repeat3 += top >= 3
        report = lint_trace(trace, rules, registry)
        exit_codes[report.exit_code] += 1
        if report.has_hard_defect:
            traj_with_defect += 1
        if report.by_tier(ConfidenceTier.HARD_EVENT):
            traj_with_event += 1
        fired = set()
        for f in report.active_findings:
            rule_tier_findings[(f.rule, f.tier.value)] += 1
            fired.add(f.rule)
            key = f"{f.rule}/{f.tier.value}"
            if key not in examples and f.summary:
                examples[key] = f"[{f.tier.value}] {f.rule} {f.finding_type}: {f.summary[:160]}"
        for r in fired:
            rule_traj[r] += 1

    print(f"=== SWE-bench precision probe: {n_traj} verified trajectories, {n_calls} tool calls ===")
    print(f"exit codes: {dict(sorted(exit_codes.items()))}  (0=pass, 1=gate, 2=hard_defect)")
    print(f"trajectories with any hard_defect: {traj_with_defect}/{n_traj}")
    print(f"trajectories with any hard_event : {traj_with_event}/{n_traj}")
    print(
        f"retries present: {traj_with_repeat2}/{n_traj} have an identical call >=2x, "
        f"{traj_with_repeat3}/{n_traj} >=3x (R4's loop threshold) -> R4 had ample chances to fire"
    )
    print("\nper-rule: trajectories firing / total findings  (and per-100-calls rate):")
    for rule in sorted(rule_traj, key=lambda r: -rule_traj[r]):
        total = sum(v for (rr, _), v in rule_tier_findings.items() if rr == rule)
        tiers = {t: v for (rr, t), v in rule_tier_findings.items() if rr == rule}
        rate = 100.0 * total / n_calls if n_calls else 0
        print(f"  {rule:4} {rule_traj[rule]:>3}/{n_traj} traj | {total:>4} findings ({rate:4.1f}/100 calls) | tiers {tiers}")
    print("\nexample finding per rule/tier:")
    for k in sorted(examples):
        print("  " + examples[k])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
