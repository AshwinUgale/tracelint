"""The DeepEval integration: tracelint's checks exposed as a DeepEval metric.

The pure core (:func:`score_trace`, :func:`resolve_trace`, :func:`resolve_registry`) needs no SDK
and is tested directly. ``TracelintMetric`` is exercised against a stand-in ``BaseMetric`` injected
into ``sys.modules`` — the contract tracelint relies on (``measure`` / ``a_measure`` /
``is_successful`` / ``__name__``, and the ``score`` / ``success`` / ``reason`` / ``error``
attributes) — so the real, heavy DeepEval package is never required to run the suite.
"""

from __future__ import annotations

import asyncio
import sys
import types

import pytest

import tracelint.integrations.deepeval as de
from tracelint.agent import ReActAgent, ScriptedLLM, build_demo_toolset, final, tool
from tracelint.findings import ConfidenceTier


def _toolset():
    return build_demo_toolset()


def _run(script, run_id):
    ts = _toolset()
    return ReActAgent(ScriptedLLM(script), ts).run("x", run_id=run_id), ts.to_registry()


def _clean():
    return _run([tool("get_order", {"order_id": 4521}), final("ok")], "clean")


def _defect():
    # cancel_order with an order id nothing supplied -> R1 schema violation (hard_defect).
    return _run([tool("cancel_order", {"order_id": 4521, "reason": "fraud"}), final("done")], "bad")


def _candidate_only():
    # three identical get_order calls -> R4 loop (candidate), no hard_defect.
    return _run([tool("get_order", {"order_id": 1})] * 3 + [final("done")], "loopy")


# --- pure core (no DeepEval) -----------------------------------------------------------


def test_score_trace_clean_passes():
    trace, reg = _clean()
    result = de.score_trace(trace, registry=reg)
    assert result.score == 1.0
    assert result.success is True
    assert result.report.exit_code == 0
    assert "passed" in result.reason


def test_score_trace_defect_fails():
    trace, reg = _defect()
    result = de.score_trace(trace, registry=reg)
    assert result.score == 0.0
    assert result.success is False
    assert result.report.has_hard_defect
    assert "R1" in result.reason and "failed" in result.reason


def test_fail_on_lowers_the_gate():
    trace, reg = _candidate_only()
    assert not de.score_trace(trace, registry=reg).report.has_hard_defect
    # a candidate doesn't gate by default ...
    assert de.score_trace(trace, registry=reg).success is True
    # ... but fail_on=candidate makes it fail, mirroring `tracelint check --fail-on candidate`.
    strict = de.score_trace(trace, registry=reg, fail_on=ConfidenceTier.CANDIDATE)
    assert strict.success is False and strict.score == 0.0


def test_resolve_trace_from_path_and_errors(tmp_path):
    trace, _ = _clean()
    one = tmp_path / "one.json"
    one.write_text(trace.to_json(), encoding="utf-8")
    assert de.resolve_trace(one).run_id == "clean"
    assert de.resolve_trace(trace) is trace  # a Trace passes through untouched

    many = tmp_path / "many.jsonl"
    many.write_text(trace.to_json(indent=None) + "\n" + trace.to_json(indent=None) + "\n", "utf-8")
    with pytest.raises(ValueError, match="traces"):
        de.resolve_trace(many)
    with pytest.raises(ValueError, match="Trace or a path"):
        de.resolve_trace(123)  # type: ignore[arg-type]


def test_resolve_registry(tmp_path):
    from tracelint.tools import ToolRegistry

    reg = _toolset().to_registry()
    assert de.resolve_registry(reg) is reg
    assert isinstance(de.resolve_registry(None), ToolRegistry)
    import json

    tp = tmp_path / "tools.json"
    tp.write_text(json.dumps({"tools": {"get_order": {"schema": {"type": "object"}}}}), "utf-8")
    assert de.resolve_registry(tp).get("get_order") is not None


def test_import_base_metric_without_sdk_raises(monkeypatch):
    monkeypatch.delitem(sys.modules, "deepeval", raising=False)
    monkeypatch.delitem(sys.modules, "deepeval.metrics", raising=False)
    with pytest.raises(RuntimeError, match=r"tracelint\[deepeval\]"):
        de._import_base_metric()


# --- TracelintMetric against a stand-in BaseMetric -------------------------------------


@pytest.fixture
def fake_deepeval(monkeypatch):
    """Inject a minimal ``deepeval.metrics.BaseMetric`` so the metric builds without the SDK."""
    metrics = types.ModuleType("deepeval.metrics")

    class BaseMetric:  # the real one carries more, but the metric only needs to subclass it
        pass

    metrics.BaseMetric = BaseMetric
    monkeypatch.setitem(sys.modules, "deepeval", types.ModuleType("deepeval"))
    monkeypatch.setitem(sys.modules, "deepeval.metrics", metrics)
    monkeypatch.setattr(de, "_METRIC_CLASS", None, raising=False)
    yield de
    monkeypatch.setattr(de, "_METRIC_CLASS", None, raising=False)


class _Case:
    """A stand-in LLMTestCase carrying only what the metric reads."""

    def __init__(self, metadata=None):
        self.additional_metadata = metadata


def test_metric_scores_a_bound_trace(fake_deepeval):
    trace, reg = _defect()
    metric = fake_deepeval.TracelintMetric(trace=trace, tools=reg)
    assert metric.measure(_Case()) == 0.0
    assert metric.is_successful() is False
    assert metric.__name__ == "tracelint"
    assert metric.threshold == 0.5
    assert "R1" in metric.reason


def test_metric_reads_trace_from_test_case_metadata(fake_deepeval):
    clean, reg = _clean()
    metric = fake_deepeval.TracelintMetric(tools=reg)
    assert metric.measure(_Case({"tracelint_trace": clean})) == 1.0
    assert metric.is_successful() is True


def test_metric_accepts_fail_on_string(fake_deepeval):
    trace, reg = _candidate_only()
    metric = fake_deepeval.TracelintMetric(trace=trace, tools=reg, fail_on="candidate")
    assert metric.measure(_Case()) == 0.0  # candidate gates under fail_on="candidate"


def test_metric_missing_trace_sets_error_not_crash(fake_deepeval):
    metric = fake_deepeval.TracelintMetric()
    assert metric.measure(_Case()) == 0.0
    assert metric.is_successful() is False
    assert metric.error and "no trace" in metric.error


def test_metric_bad_trace_path_sets_error_not_crash(fake_deepeval):
    metric = fake_deepeval.TracelintMetric(trace="does-not-exist.json")
    assert metric.measure(_Case()) == 0.0
    assert metric.error  # the load failure is captured, the suite keeps running


def test_a_measure_matches_measure(fake_deepeval):
    clean, reg = _clean()
    metric = fake_deepeval.TracelintMetric(trace=clean, tools=reg)
    assert asyncio.run(metric.a_measure(_Case())) == metric.measure(_Case()) == 1.0


def test_unknown_attribute_still_raises():
    with pytest.raises(AttributeError):
        de.NoSuchThing  # noqa: B018 - PEP 562 __getattr__ must reject unknown names
