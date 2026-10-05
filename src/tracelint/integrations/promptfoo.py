"""promptfoo integration — run tracelint as a promptfoo Python assertion.

promptfoo (https://promptfoo.dev) drives evals from YAML and checks each output with assertions. A
``type: python`` assertion names a file whose ``get_assert(output, context)`` returns the verdict.
This module *is* that file: reference it from a promptfoo config and tracelint's deterministic
checks become a pass/fail assertion, with the same gate as ``tracelint check`` — judge-free.

tracelint scores a *trace*, not the LLM's text output, so the assertion finds the trace via, in
order: the assertion's own ``config.trace``; a ``vars`` entry named by ``config.trace_var`` (default
``tracelint_trace``); or the ``output`` when it is itself a trace (a :class:`Trace`, or a path to an
existing trace file). Everything else — ``tools``, ``rules``, ``fmt``, ``fail_on`` — is read from
the assertion's ``config``, so a whole check is expressible in YAML with no Python:

    assert:
      - type: python
        value: file://path/to/tracelint/integrations/promptfoo.py
        config:
          tools: tools.json
          fail_on: hard_defect
          trace_var: tracelint_trace

or, if you prefer a local file, a one-liner that re-exports it::

    from tracelint.integrations.promptfoo import get_assert  # noqa: F401

promptfoo is a Node tool and imports no Python SDK — it just calls ``get_assert`` — so this module
has no extra to install beyond tracelint itself. The scoring core is shared with the other harness
wrappers (see :mod:`tracelint.integrations.scoring`).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tracelint.findings import ConfidenceTier
from tracelint.integrations.scoring import (
    DEFAULT_TRACE_KEY,
    as_tier,
    resolve_registry,
    resolve_trace,
    score_trace,
)
from tracelint.rules import select_rules
from tracelint.trace import Trace

__all__ = ["assert_trace", "get_assert"]


def _grade(passed: bool, score: float, reason: str) -> dict[str, Any]:
    """A promptfoo GradingResult dict."""
    return {"pass": passed, "score": score, "reason": reason}


def _context_get(context: Any, key: str, default: Any = None) -> Any:
    # promptfoo passes `context` as a dict to Python assertions; tolerate an object too.
    if isinstance(context, dict):
        return context.get(key, default)
    return getattr(context, key, default)


def _find_trace(output: Any, context: Any, *, trace_var: str, config_trace: Any) -> Any:
    """Locate the trace to check: explicit ``config.trace``, then ``vars[trace_var]``, then an
    ``output`` that is itself a Trace or a path to an existing file. ``None`` if none is found."""
    if config_trace is not None:
        return config_trace
    vars_ = _context_get(context, "vars", {}) or {}
    if isinstance(vars_, dict) and vars_.get(trace_var) is not None:
        return vars_[trace_var]
    if isinstance(output, Trace):
        return output
    if isinstance(output, (str, Path)):
        text = str(output).strip()
        if text:
            path = Path(text)
            if path.exists() and path.is_file():
                return path
    return None


def assert_trace(
    output: Any,
    context: Any = None,
    *,
    tools: Any = None,
    rules: list[str] | None = None,
    fmt: str = "native",
    fail_on: str | ConfidenceTier = ConfidenceTier.HARD_DEFECT,
    trace_var: str = DEFAULT_TRACE_KEY,
    trace: Any = None,
) -> dict[str, Any]:
    """Score the run's trace, returning a GradingResult (``pass`` / ``score`` / ``reason``).

    A missing trace, or one that won't load, fails the assertion with the reason recorded — it never
    raises into promptfoo. ``pass`` is exactly ``tracelint check``'s clean exit under ``fail_on``.
    """
    source = _find_trace(output, context, trace_var=trace_var, config_trace=trace)
    if source is None:
        return _grade(
            False,
            0.0,
            "tracelint: no trace to check — set the assertion's config.trace, a vars entry "
            f"{trace_var!r}, or have the provider output a path to a trace file.",
        )
    try:
        resolved = resolve_trace(source, fmt=fmt)
        result = score_trace(
            resolved,
            registry=resolve_registry(tools),
            rules=select_rules(rules),
            fail_on=as_tier(fail_on),
        )
    except Exception as exc:  # a bad path / unreadable trace shouldn't error the whole eval
        return _grade(False, 0.0, f"tracelint could not check the trace: {exc}")
    return _grade(result.success, result.score, result.reason)


def get_assert(output: Any, context: Any = None) -> dict[str, Any]:
    """promptfoo's entry point. Reads the check's settings from the assertion's ``config`` block."""
    config = _context_get(context, "config", {}) or {}
    return assert_trace(
        output,
        context,
        tools=config.get("tools"),
        rules=config.get("rules"),
        fmt=config.get("fmt", "native"),
        fail_on=config.get("fail_on", ConfidenceTier.HARD_DEFECT),
        trace_var=config.get("trace_var", DEFAULT_TRACE_KEY),
        trace=config.get("trace"),
    )
