"""The Inspect Scout scanners (needs ``pip install "tracelint[scout]"``).

Each scanner reads a transcript's messages and its ``tool`` events, lints them with
:func:`~tracelint.integrations.scout.lint_messages`, and returns Scout ``Result`` s that cite the
messages each finding is about. They are registered as ``tracelint/<name>`` through the
``inspect_ai`` entry point, and can also be used from Python::

    from inspect_scout import scan, transcripts_from
    from tracelint.integrations.scout.scanners import tool_loops, tracelint

    scan([tracelint(tools="tools.json"), tool_loops()], transcripts=transcripts_from("./logs"))

Every scanner takes ``tools`` (a ``tools.json`` path or its dict; optional) and ``min_tier``
(``candidate`` / ``hard_event`` / ``hard_defect``: the lowest tier counted, default every tier).
The result shapes are described in :mod:`tracelint.integrations.scout`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from inspect_scout import Reference, Result, Scanner, Transcript, scanner

from tracelint.findings import ConfidenceTier
from tracelint.integrations.scout import (
    Citation,
    RuleResult,
    lint_messages,
    resolve_contract,
    resolve_rules,
)
from tracelint.tools import ToolRegistry

try:  # Scout's id for a message recorded without one, so its citation still links
    from inspect_scout._scanner.util import _message_id
except ImportError:  # pragma: no cover - a Scout version without the helper
    _message_id = None

Tools = str | Path | dict[str, Any] | None


def _reference(citation: Citation, transcript: Transcript) -> Reference | None:
    message_id = citation.message_id
    if message_id is None and _message_id is not None:
        message_id = _message_id(transcript.messages[citation.position])
    if message_id is None:
        return None
    return Reference(type="message", cite=citation.cite, id=message_id)


def _result(rule: RuleResult, transcript: Transcript, *, labelled: bool) -> Result:
    references = [_reference(c, transcript) for c in rule.citations]
    return Result(
        value=rule.value,
        explanation=rule.explanation,
        references=[r for r in references if r is not None],
        metadata=rule.metadata,
        label=rule.label if labelled else None,
    )


def _scan_fn(rules: Any, tools: Tools, min_tier: str, *, labelled: bool) -> Any:
    """The scan function: the contract and rules are resolved once, here, so bad arguments fail
    when the scanner is created rather than on every transcript."""
    registry: ToolRegistry | None = resolve_contract(tools)
    chosen = resolve_rules(rules, registry)
    floor = ConfidenceTier(min_tier)

    def lint(transcript: Transcript) -> list[RuleResult]:
        return lint_messages(
            transcript.messages,
            events=transcript.events,
            rules=chosen,
            tools=registry,
            min_tier=floor,
            run_id=transcript.transcript_id,
        )

    if labelled:

        async def scan_rules(transcript: Transcript) -> list[Result]:
            return [_result(o, transcript, labelled=True) for o in lint(transcript)]

        return scan_rules

    async def scan_rule(transcript: Transcript) -> Result:
        (outcome,) = lint(transcript)
        return _result(outcome, transcript, labelled=False)

    return scan_rule


@scanner(messages="all", events=["tool"])
def tracelint(
    rules: list[str] | str | None = None,
    tools: Tools = None,
    min_tier: str = "candidate",
) -> Scanner[Transcript]:
    """tracelint's deterministic trajectory rules, one labelled result per rule.

    Args:
        rules: Rule ids to run (a list, or ``"R4,R5"``). Default: R2a, R4, R5 and R6, plus R1, R7,
            R8, R9, R10, R12 and R13 when ``tools`` is given (R13 only if it declares
            ``off_limits``).
        tools: A ``tools.json`` contract (``tracelint init`` / ``init --from-mcp`` write one), as a
            path or its parsed dict.
        min_tier: The lowest confidence tier counted: ``candidate`` (default), ``hard_event`` or
            ``hard_defect``.
    """
    return _scan_fn(rules, tools, min_tier, labelled=True)


@scanner(messages="all", events=["tool"])
def tool_errors(tools: Tools = None, min_tier: str = "candidate") -> Scanner[Transcript]:
    """R2a: tool calls that returned an error — structured errors (Inspect's ``ToolCallError`` on
    the message or its tool event, an ``error`` field) as ``hard_event``, error text in an
    unclassified result as ``candidate``."""
    return _scan_fn(["R2a"], tools, min_tier, labelled=False)


@scanner(messages="all", events=["tool"])
def tool_loops(tools: Tools = None, min_tier: str = "candidate") -> Scanner[Transcript]:
    """R4: the same call repeated with no progress in its result (a stuck loop): ``candidate``."""
    return _scan_fn(["R4"], tools, min_tier, labelled=False)


@scanner(messages="all", events=["tool"])
def redundant_calls(tools: Tools = None, min_tier: str = "candidate") -> Scanner[Transcript]:
    """R5: a call repeating an earlier identical call that already returned the same result."""
    return _scan_fn(["R5"], tools, min_tier, labelled=False)


@scanner(messages="all", events=["tool"])
def malformed_arguments(tools: Tools = None, min_tier: str = "candidate") -> Scanner[Transcript]:
    """R6: tool calls whose arguments the model emitted as invalid JSON (``hard_defect``)."""
    return _scan_fn(["R6"], tools, min_tier, labelled=False)


__all__ = ["malformed_arguments", "redundant_calls", "tool_errors", "tool_loops", "tracelint"]
