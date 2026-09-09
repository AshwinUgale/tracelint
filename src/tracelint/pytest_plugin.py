"""A pytest fixture that makes tracelint a native test assertion.

Opt in from your ``conftest.py`` (this is not an auto-registered plugin — a linter shouldn't inject
a global fixture into every project that installs it):

    pytest_plugins = ["tracelint.pytest_plugin"]

Then capture and lint an agent run inside a test:

    def test_agent(trace_capture):
        with trace_capture(framework="smolagents"):
            agent.run("refund order A100")     # your agent under test, unchanged
        # on exit the run's trace is linted; a hard defect fails the test

It's a thin wrapper over :func:`tracelint.capture.capture` (which records the run) and
:func:`tracelint.lint_otel_trace` (which lints the captured spans). Capturing a real framework needs
that framework's extra, e.g. ``pip install "tracelint[capture-smolagents]"``.

The fixture auto-fails the test on a **hard defect**; heuristic candidates never fail it. Pass
``assert_clean=False`` to inspect the report yourself instead, and read it from the handle's
``.report`` after the block. For a manual/advanced run (no framework), omit ``framework`` and emit
spans on the handle's ``.tracer``.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from tracelint import lint_otel_trace
from tracelint.capture import capture
from tracelint.findings import ConfidenceTier, LintReport
from tracelint.tools import ToolRegistry


class TraceCapture:
    """Handle yielded inside the ``with`` block.

    ``tracer`` is the OpenTelemetry tracer for the capture (useful only for a manual run with no
    ``framework``); ``report`` is the :class:`~tracelint.findings.LintReport`, populated once the
    block exits and the captured trace has been linted.
    """

    def __init__(self, tracer: Any) -> None:
        self.tracer = tracer
        self.report: LintReport | None = None


@pytest.fixture
def trace_capture(tmp_path: Path):
    """Capture an agent run and lint it; a hard defect fails the test.

    Yields a factory — ``trace_capture(framework=..., assert_clean=True, registry=None)`` returns a
    context manager. On exit the captured trace is linted with :func:`tracelint.lint_otel_trace`;
    unless ``assert_clean=False`` a hard defect raises ``AssertionError`` (failing the test). The
    report is available as the handle's ``.report`` afterwards.
    """
    counter = {"n": 0}

    @contextmanager
    def _trace_capture(
        framework: str | None = None,
        *,
        assert_clean: bool = True,
        registry: ToolRegistry | None = None,
    ) -> Iterator[TraceCapture]:
        counter["n"] += 1
        path = tmp_path / f"trace_{counter['n']}.json"
        handle = TraceCapture(tracer=None)
        with capture(path, framework=framework) as tracer:
            handle.tracer = tracer
            yield handle
        # The capture block has closed, so the spans are on disk — lint them now.
        spans = json.loads(path.read_text(encoding="utf-8"))
        handle.report = lint_otel_trace(spans, registry=registry)
        if assert_clean and handle.report.has_hard_defect:
            hard = handle.report.by_tier(ConfidenceTier.HARD_DEFECT)
            detail = "; ".join(f"{f.rule} {f.summary}" for f in hard) or "see the report"
            raise AssertionError(f"tracelint: the agent trace has a hard defect ({detail})")

    return _trace_capture
