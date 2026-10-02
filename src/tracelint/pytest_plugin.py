"""A pytest fixture that makes tracelint a native test assertion.

Opt in from your ``conftest.py`` (this is not an auto-registered plugin — a linter shouldn't inject
a global fixture into every project that installs it):

    pytest_plugins = ["tracelint.pytest_plugin"]

Then capture and lint an agent run inside a test:

    def test_agent(trace_capture):
        with trace_capture(framework="smolagents"):
            agent.run("refund order A100")     # your agent under test, unchanged
        # on exit the run's trace is linted; a hard defect fails the test

It's a thin wrapper over :func:`tracelint.capture.capture` (which records the run) and the same
loader ``tracelint check --format openinference`` uses (which lints each captured run). Capturing a
real framework needs that framework's extra, e.g. ``pip install "tracelint[capture-smolagents]"``.

The fixture auto-fails the test on a **hard defect**; heuristic candidates never fail it. Pass
``assert_clean=False`` to inspect the report yourself instead, and read it from the handle's
``.report`` after the block (``.reports`` holds one per run when the block ran the agent more than
once). A capture with nothing to lint fails the test too — an empty capture is a broken setup, not
a clean run. For a manual/advanced run (no framework), omit ``framework`` and emit spans on the
handle's ``.tracer``.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from tracelint.capture import capture
from tracelint.findings import ConfidenceTier, LintReport
from tracelint.rules import default_rules, lint_trace
from tracelint.sources import OPENINFERENCE, load_source
from tracelint.tools import ToolRegistry


class TraceCapture:
    """Handle yielded inside the ``with`` block.

    ``tracer`` is the OpenTelemetry tracer for the capture (useful only for a manual run with no
    ``framework``). ``reports`` holds one :class:`~tracelint.findings.LintReport` per captured run,
    populated once the block exits; ``report`` is the report of the block's one run.
    """

    def __init__(self, tracer: Any) -> None:
        self.tracer = tracer
        self.reports: list[LintReport] = []

    @property
    def report(self) -> LintReport | None:
        """The run's report (``None`` until the block exits). A block that ran the agent more than
        once has one report per run in :attr:`reports`."""
        if len(self.reports) > 1:
            raise ValueError(
                f"this capture recorded {len(self.reports)} runs — read .reports (one per run)"
            )
        return self.reports[0] if self.reports else None


@pytest.fixture
def trace_capture(tmp_path: Path):
    """Capture an agent run and lint it; a hard defect fails the test.

    Yields a factory — ``trace_capture(framework=..., assert_clean=True, registry=None)`` returns a
    context manager. On exit each captured run is linted; unless ``assert_clean=False`` a hard
    defect in any run raises ``AssertionError`` (failing the test), as does a capture with nothing
    to lint. The reports are on the handle afterwards (``.report``, or ``.reports`` per run).
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
        # capture() raises if it recorded nothing; once the block closes the spans are on disk.
        with capture(path, framework=framework) as tracer:
            handle.tracer = tracer
            yield handle
        try:
            traces = load_source(path, OPENINFERENCE)  # one per run, as `tracelint check` reads it
        except ValueError as exc:  # spans, but no tool calls or messages among them
            message = f"tracelint: the captured run has nothing to lint ({exc})"
            raise AssertionError(message) from exc
        handle.reports = [lint_trace(trace, default_rules(), registry) for trace in traces]
        hard = [f for r in handle.reports for f in r.by_tier(ConfidenceTier.HARD_DEFECT)]
        if assert_clean and hard:
            detail = "; ".join(f"{f.rule} {f.summary}" for f in hard)
            raise AssertionError(f"tracelint: the agent trace has a hard defect ({detail})")

    return _trace_capture
