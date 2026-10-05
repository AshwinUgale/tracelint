#!/usr/bin/env python3
"""Are the *findings* any good? Quality (not just gating) of the candidate findings.

Uses a ground-truth signal tracelint never saw: each observation's `<returncode>`. For an R2a
"tool error" finding, if the flagged result's command returned exit 0, the tool SUCCEEDED — so the
finding is a provable false positive (it matched an error-word in normal output). For R3, we measure
whether it fires on essentially every call (no discriminating power). Prints FP estimates with the
returncode evidence, plus sampled finding texts.
"""

from __future__ import annotations

import re
import sys
from collections import Counter
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from run_precision import messages_to_trace  # noqa: E402

from tracelint.rules import default_rules, lint_trace  # noqa: E402
from tracelint.tools import ToolRegistry  # noqa: E402
from tracelint.trace import ToolCall, ToolResult  # noqa: E402

RC = re.compile(r"<returncode>(-?\d+)</returncode>")


def returncode(content) -> int | None:
    m = RC.search(content or "") if isinstance(content, str) else None
    return int(m.group(1)) if m else None


def main() -> int:
    shard = Path(sys.argv[1])
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 200
    df = pd.read_parquet(shard)
    pool = df[(df["verified"]) & (df["exit_status"].str.lower() == "submitted")]
    import random

    idx = random.Random(20261005).sample(range(len(pool)), min(n, len(pool)))
    rows = pool.iloc[idx]
    rules = default_rules()
    registry = ToolRegistry()

    n_calls = 0
    r2a_rc: Counter = Counter()  # returncode bucket of R2a-flagged results
    r3_calls_flagged = 0
    r3_samples, r5_samples = [], []
    r2a_fp_samples = []

    for _, row in rows.iterrows():
        trace = messages_to_trace(row["messages"], str(row["instance_id"]))
        calls = [s for s in trace.steps if isinstance(s, ToolCall)]
        if not calls:
            continue
        n_calls += len(calls)
        cmd_by_call = {s.call_id: s.args.get("command", "?") for s in calls}
        report = lint_trace(trace, rules, registry)
        r3_hit_calls = set()
        for f in report.active_findings:
            steps = [trace.steps[i] for i in f.step_indices if 0 <= i < len(trace.steps)]
            if f.rule == "R2a":
                res = next((s for s in steps if isinstance(s, ToolResult)), None)
                rc = returncode(res.content) if res else None
                bucket = "exit 0 (SUCCESS -> provable FP)" if rc == 0 else (
                    f"exit {rc}" if rc is not None else "no returncode"
                )
                r2a_rc[bucket] += 1
                if rc == 0 and res and len(r2a_fp_samples) < 5:
                    cmd = str(cmd_by_call.get(res.call_id, "?")).replace("\n", " ")[:70]
                    r2a_fp_samples.append(f"exit 0, flagged as tool error: {cmd!r}")
            elif f.rule == "R3":
                for i in f.step_indices:
                    r3_hit_calls.add(i)
                if len(r3_samples) < 3:
                    r3_samples.append(f.summary[:150])
            elif f.rule == "R5" and len(r5_samples) < 3:
                r5_samples.append(f.summary[:170])
        r3_calls_flagged += len({i for i in r3_hit_calls})

    total_r2a = sum(r2a_rc.values())
    print(f"=== findings quality over {n_calls} calls ===\n")
    print(f"R2a (tool error) findings: {total_r2a}")
    for bucket, c in r2a_rc.most_common():
        print(f"   {c:>5} ({100*c/total_r2a:4.1f}%)  {bucket}")
    fp = r2a_rc.get("exit 0 (SUCCESS -> provable FP)", 0)
    print(f"   -> provable false positives (command returned exit 0): {fp}/{total_r2a} = {100*fp/total_r2a:.0f}%")
    print(f"\nR3 (hallucinated arg): flagged {r3_calls_flagged} distinct call-steps; ~{100*r3_calls_flagged/n_calls:.0f}% of all calls")
    print("   -> fires on ~every command => no discriminating power on free-form shell args")
    print("\nsample R2a false positives (exit 0 but flagged):")
    for s in r2a_fp_samples:
        print("   " + s)
    print("\nsample R3 findings:")
    for s in r3_samples:
        print("   " + s)
    print("\nsample R5 findings:")
    for s in r5_samples:
        print("   " + s)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
