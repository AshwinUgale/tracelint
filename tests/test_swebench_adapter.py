"""The OpenHands SWE-bench trajectory adapter (experiments/swebench/adapter.py).

Validated against the real submission format (OpenAI chat messages with ``tool_calls`` and a
text-embedded exit code); this exercises the mapping on a synthetic trajectory of that exact shape,
so no third-party trajectory data is committed.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "experiments"))

from swebench.adapter import extract_exit_code, trajectory_to_trace  # noqa: E402

from tracelint.trace import Message, ResultStatus, Role, ToolCall, ToolResult  # noqa: E402


def _call(call_id, name, args, text=""):
    return {
        "role": "assistant",
        "content": [{"type": "text", "text": text}] if text else [],
        "tool_calls": [
            {"id": call_id, "type": "function",
             "function": {"name": name, "arguments": json.dumps(args)}}
        ],
    }


def _obs(call_id, name, text):
    return {"role": "tool", "tool_call_id": call_id, "name": name,
            "content": [{"type": "text", "text": text}]}


MESSAGES = [
    {"role": "system", "content": [{"type": "text", "text": "You are OpenHands."}]},
    {"role": "user", "content": [{"type": "text", "text": "<issue>Fix the bug in foo.py</issue>"}]},
    _call("t0", "think", {"thought": "Let me look around the repo."}),
    _obs("t0", "think", "Your thought has been logged."),
    _call("c1", "execute_bash", {"command": "ls -la"}, text="Listing the files."),
    _obs("c1", "execute_bash", "foo.py\nbar.py\n[The command completed with exit code 0.]"),
    _call("c2", "execute_bash", {"command": "python -m pytest -q"}),
    _obs("c2", "execute_bash", "1 failed\n[The command completed with exit code 1.]"),
    _call("c3", "str_replace_editor", {"command": "view", "path": "/foo.py"}),
    _obs("c3", "str_replace_editor", "1\tdef foo():\n2\t    pass"),  # no exit marker -> unknown
]


def _trace():
    return trajectory_to_trace(MESSAGES, run_id="sub/inst-1")


def test_run_id_and_tool_calls_exclude_think():
    tr = _trace()
    assert tr.run_id == "sub/inst-1"
    names = [s.name for s in tr.steps if isinstance(s, ToolCall)]
    assert names == ["execute_bash", "execute_bash", "str_replace_editor"]


def test_think_becomes_a_thought_message():
    tr = _trace()
    assert not any(isinstance(s, ToolCall) and s.name == "think" for s in tr.steps)
    assert any(
        isinstance(s, Message) and s.role is Role.ASSISTANT and "look around" in s.content
        for s in tr.steps
    )


def test_command_preserved_and_result_paired():
    tr = _trace()
    call = next(
        s for s in tr.steps if isinstance(s, ToolCall) and s.args.get("command") == "ls -la"
    )
    result = tr.result_for(call)
    assert result is not None and "foo.py" in result.content


def test_status_comes_from_the_exit_code():
    by_id = {s.call_id: s for s in _trace().steps if isinstance(s, ToolResult)}
    assert by_id["c1"].status is ResultStatus.OK  # exit 0
    assert by_id["c2"].status is ResultStatus.ERROR  # exit 1
    assert by_id["c3"].status is ResultStatus.UNKNOWN  # no marker in the observation


def test_extract_exit_code_takes_the_last_marker():
    text = "x\n[The command completed with exit code 0.]\n[Command finished with exit code 2.]"
    assert extract_exit_code(text) == 2
    assert extract_exit_code("no marker here") is None


def test_user_issue_is_seeded_for_provenance():
    tr = _trace()
    assert any(
        isinstance(s, Message) and s.role is Role.USER and "Fix the bug" in s.content
        for s in tr.steps
    )
