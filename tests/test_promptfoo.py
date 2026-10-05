"""The promptfoo integration: tracelint's checks exposed as a promptfoo Python assertion.

promptfoo calls ``get_assert(output, context)`` and expects a GradingResult (``pass`` / ``score`` /
``reason``). These tests drive that contract directly — the trace found via ``config.trace``, a
``vars`` entry, or the ``output`` itself — with no promptfoo install (it is a Node tool that just
invokes the function).
"""

from __future__ import annotations

import json

from tracelint.agent import ReActAgent, ScriptedLLM, build_demo_toolset, final, tool
from tracelint.integrations import promptfoo as pf


def _toolset():
    return build_demo_toolset()


def _trace_file(tmp_path, name, script, run_id):
    ts = _toolset()
    trace = ReActAgent(ScriptedLLM(script), ts).run("x", run_id=run_id)
    path = tmp_path / name
    path.write_text(trace.to_json(), encoding="utf-8")
    return path


def _tools_file(tmp_path):
    ts = _toolset()
    specs = {n: {"schema": ts.to_registry().get(n).schema} for n in ts.names()}
    path = tmp_path / "tools.json"
    path.write_text(json.dumps({"tools": specs}), encoding="utf-8")
    return path


def _clean(tmp_path):
    return _trace_file(
        tmp_path, "clean.json", [tool("get_order", {"order_id": 4521}), final("ok")], "clean"
    )


def _defect(tmp_path):
    return _trace_file(
        tmp_path,
        "bad.json",
        [tool("cancel_order", {"order_id": 4521, "reason": "fraud"}), final("x")],
        "bad",
    )


def _loop(tmp_path):
    return _trace_file(
        tmp_path, "loop.json", [tool("get_order", {"order_id": 1})] * 3 + [final("x")], "loopy"
    )


def test_grading_result_shape(tmp_path):
    result = pf.get_assert("out", {"vars": {"tracelint_trace": str(_clean(tmp_path))}})
    assert set(result) == {"pass", "score", "reason"}
    assert isinstance(result["pass"], bool) and isinstance(result["score"], float)
    assert isinstance(result["reason"], str)


def test_defect_fails_via_vars_and_config(tmp_path):
    ctx = {
        "vars": {"tracelint_trace": str(_defect(tmp_path))},
        "config": {"tools": str(_tools_file(tmp_path))},
    }
    result = pf.get_assert("ignored output", ctx)
    assert result["pass"] is False
    assert result["score"] == 0.0
    assert "R1" in result["reason"]


def test_clean_passes(tmp_path):
    ctx = {"vars": {"tracelint_trace": str(_clean(tmp_path))}}
    assert pf.get_assert("ignored", ctx)["pass"] is True


def test_trace_from_config_trace(tmp_path):
    # config.trace takes precedence and needs no vars entry.
    ctx = {"config": {"trace": str(_defect(tmp_path)), "tools": str(_tools_file(tmp_path))}}
    assert pf.get_assert("x", ctx)["pass"] is False


def test_trace_from_output_path(tmp_path):
    # the provider's output is itself a path to a trace file.
    path = _defect(tmp_path)
    assert (
        pf.get_assert(str(path), {"config": {"tools": str(_tools_file(tmp_path))}})["pass"] is False
    )


def test_no_trace_is_a_clean_fail(tmp_path):
    result = pf.get_assert("just the model's text answer", {"config": {}})
    assert result["pass"] is False
    assert "no trace" in result["reason"]


def test_unreadable_trace_is_a_clean_fail(tmp_path):
    result = pf.get_assert("x", {"vars": {"tracelint_trace": str(tmp_path / "missing.json")}})
    assert result["pass"] is False
    assert "could not" in result["reason"] or "no trace" in result["reason"]


def test_fail_on_in_config_lowers_the_gate(tmp_path):
    loop, tools = str(_loop(tmp_path)), str(_tools_file(tmp_path))
    # a candidate doesn't gate by default ...
    assert (
        pf.get_assert("x", {"vars": {"tracelint_trace": loop}, "config": {"tools": tools}})["pass"]
        is True
    )
    # ... but fail_on=candidate makes it fail.
    strict = {"vars": {"tracelint_trace": loop}, "config": {"tools": tools, "fail_on": "candidate"}}
    assert pf.get_assert("x", strict)["pass"] is False


def test_context_may_be_an_object(tmp_path):
    # promptfoo passes a dict, but tolerate an attribute-style context too.
    class Ctx:
        vars = None
        config = None

    ctx = Ctx()
    ctx.vars = {"tracelint_trace": str(_clean(tmp_path))}
    ctx.config = {}
    assert pf.get_assert("x", ctx)["pass"] is True


def test_assert_trace_direct_kwargs(tmp_path):
    # the explicit-kwargs core, for a user who writes their own get_assert wrapper.
    result = pf.assert_trace(
        "x",
        {"vars": {"tracelint_trace": str(_defect(tmp_path))}},
        tools=str(_tools_file(tmp_path)),
        fail_on="hard_defect",
    )
    assert result["pass"] is False
