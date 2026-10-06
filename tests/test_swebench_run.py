"""The SWE-bench experiment runner's metric helpers (experiments/swebench/run.py)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "experiments"))

from swebench.adapter import trajectory_to_trace  # noqa: E402
from swebench.run import _bash_stats, _counts  # noqa: E402

from tracelint.rules import default_rules, lint_trace  # noqa: E402
from tracelint.tools import ToolRegistry  # noqa: E402


def _call(call_id, name, args):
    return {
        "role": "assistant", "content": [],
        "tool_calls": [{"id": call_id, "type": "function",
                        "function": {"name": name, "arguments": __import__("json").dumps(args)}}],
    }


def _obs(call_id, text):
    return {"role": "tool", "tool_call_id": call_id, "name": "execute_bash",
            "content": [{"type": "text", "text": text}]}


MESSAGES = [
    {"role": "user", "content": [{"type": "text", "text": "fix it"}]},
    _call("c1", "execute_bash", {"command": "ls"}),
    _obs("c1", "ok\n[The command completed with exit code 0.]"),
    _call("c2", "execute_bash", {"command": "pytest"}),
    _obs("c2", "1 failed\n[The command completed with exit code 1.]"),
    _call("c3", "str_replace_editor", {"command": "view", "path": "/a.py"}),
    _obs("c3", "1\tcode"),  # no exit marker
]


def _trace():
    return trajectory_to_trace(MESSAGES, run_id="s/i")


def test_bash_stats_counts_exit_codes():
    n_calls, n_bash, with_code, nonzero = _bash_stats(_trace())
    assert n_calls == 3  # 2 bash + 1 editor
    assert n_bash == 2
    assert with_code == 2  # both bash results carry an exit code
    assert nonzero == 1  # the pytest run exited 1


def test_counts_has_expected_columns_and_r2a_hard():
    report = lint_trace(_trace(), default_rules(), ToolRegistry())
    counts = _counts(report)
    for key in ("R2a_hard", "R1_hard", "exit_code", "suppressed", "any_hard_event"):
        assert key in counts
    # the exit-1 result is a structured error, so R2a fires as a hard_event (not a string match).
    assert counts["R2a_hard"] >= 1
    assert counts["any_hard_event"] == 1
