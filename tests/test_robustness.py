"""First-run robustness: a malformed / unfamiliar trace degrades to a clean error, never a crash.

Outreach tells people to `pip install tracelint` and point it at their own trace, so an unexpected
shape must exit with a message (exit 3), not a Python traceback.
"""

from __future__ import annotations

import json

import pytest

from tracelint.cli import main
from tracelint.findings import EXIT_INPUT_ERROR
from tracelint.trace import Trace


def _write(tmp_path, obj):
    p = tmp_path / "t.json"
    p.write_text(json.dumps(obj), encoding="utf-8")
    return str(p)


@pytest.mark.parametrize(
    "obj",
    [
        {"run_id": "x", "steps": "nope"},  # steps not a list
        {"run_id": "x", "steps": [1, 2, 3]},  # steps aren't objects
        {"run_id": "x", "steps": [{"type": "tool_call", "call_id": "c", "name": "t", "args": 5}]},
        "not even an object",  # top-level not a dict
    ],
)
def test_malformed_native_trace_exits_cleanly_not_crash(tmp_path, obj):
    # main() returns the exit code; a crash would raise instead of returning EXIT_INPUT_ERROR.
    assert main(["check", _write(tmp_path, obj)]) == EXIT_INPUT_ERROR


@pytest.mark.parametrize("fmt", ["openinference", "otel", "openai", "langfuse", "langsmith"])
def test_unfamiliar_shape_never_crashes_any_format(tmp_path, fmt):
    # A random JSON array under any adapter must not raise a traceback — clean exit (0 if it reads
    # as an empty/uneventful trace, or 3 as an input error), never an uncaught exception.
    code = main(["check", _write(tmp_path, [{"foo": "bar"}, {"baz": 1}]), "--format", fmt])
    assert code in (0, EXIT_INPUT_ERROR)


def test_from_dict_rejects_non_list_steps():
    with pytest.raises(ValueError, match="steps"):
        Trace.from_dict({"steps": "nope"})


def test_from_dict_rejects_non_object_args():
    with pytest.raises(ValueError, match="args"):
        Trace.from_dict(
            {"steps": [{"type": "tool_call", "call_id": "c", "name": "t", "args": 5}]}
        )
