"""The offline OpenInference/Phoenix example lints its planted spans deterministically."""

from __future__ import annotations

from examples.lint_openinference_phoenix import (
    build_openinference_spans,
    build_registry,
    main,
)
from tracelint import lint_otel_trace
from tracelint.findings import ConfidenceTier


def test_schema_free_run_has_no_hard_defect():
    # Without a registry, R1 suppresses (no schema), so nothing structurally-provable → exit 0.
    report = lint_otel_trace(build_openinference_spans())
    assert not report.has_hard_defect
    assert any(f.rule == "R2a" for f in report.by_tier(ConfidenceTier.HARD_EVENT))


def test_with_registry_r1_is_a_hard_defect():
    report = lint_otel_trace(build_openinference_spans(), registry=build_registry())
    defects = report.by_tier(ConfidenceTier.HARD_DEFECT)
    assert [f.rule for f in defects] == ["R1"]
    assert report.exit_code == 2


def test_main_exits_two():
    assert main() == 2
