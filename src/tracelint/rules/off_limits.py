"""R13 — off-limits source: the run requested, or searched for, a source its contract rules out.

A benchmark publishes what an agent must not use: each task's solution and tests, the task
repository and its mirrors, the leaderboard's own pages. Whether an agent reached for them is
visible in the trace, but only against a list that says which sources those are. The operator
declares it once, beside ``tools`` in ``tools.json``::

    "off_limits": {"sources": ["tbench.ai", "terminal-bench"],
                   "reason": "the benchmark publishes each task's solution"}

A call reaches a source in one of two ways, and R13 looks only at those:

- **a URL** anywhere in its arguments (a fetch tool's ``url``, a ``curl`` or ``git clone`` in a
  shell command) that names the source, percent-decoded;
- **a search query**, an argument named ``query``, ``q``, ``queries``, ``search_query``,
  ``search_term`` or ``search_queries``, that names it.

A source matches case-insensitively as a whole token: ``tbench.ai`` matches
``https://www.tbench.ai/registry`` but not ``nottbench.ai``, and in a source ``-``, ``_`` and a
space are interchangeable, so ``terminal-bench`` also matches ``terminal_bench`` and a search for
``terminal bench``. A leading scheme, ``www.`` and a trailing ``/`` are ignored.

A benchmark's name elsewhere in a call is not a request for it. On the Terminal-Bench 2.0
leaderboard runs it shows up in hundreds of runs for ordinary reasons: a task whose input is the
string ``"terminal-bench"``, calendar files whose header names the benchmark, canary strings copied
into files. So R13 never reads commands or file contents for the name itself, and a download by
package or repository name with no URL (``pip install``, ``hf download org/name``) is not seen.

A match is a ``hard_defect``: the contract declared the source off-limits and the trace shows the
call naming it. What the call got back is in the evidence (``outcome``), since a request that
failed is still an attempt. When the exact URL was given in the prompt, the finding is marked a
possible false positive: the task itself pointed there. Calls whose arguments the trace did not
record can't be checked and are disclosed as such. A contract without ``off_limits`` adds nothing.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from functools import lru_cache
from typing import Any
from urllib.parse import unquote_plus

from tracelint.findings import ConfidenceTier, Coverage, Finding
from tracelint.rules.base import Rule
from tracelint.rules.error_handling import _failed
from tracelint.tools import OffLimits, ToolRegistry
from tracelint.trace import Message, Role, ToolCall, Trace

#: Argument names that hold a search query.
QUERY_KEYS = frozenset({"query", "q", "queries", "search_query", "search_term", "search_queries"})

_URL_RE = re.compile(r"[a-z][a-z0-9+.-]*://[^\s'\"<>`]+", re.IGNORECASE)
_SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.-]*://")
_SEPARATORS = re.compile(r"[-_\s]+")
_TEXT_LIMIT = 300


@dataclass(frozen=True)
class _Match:
    surface: str  # "url" or "query"
    field: str | None  # the argument the text came from
    source: str  # the declared source it names
    text: str  # the URL or the query


@lru_cache(maxsize=256)
def source_pattern(source: str) -> re.Pattern[str]:
    """How a declared source matches: as a whole token, in any case, ``-``/``_``/space alike."""
    s = _SCHEME_RE.sub("", source.strip().lower()).removeprefix("www.").rstrip("/")
    parts = [re.escape(p) for p in _SEPARATORS.split(s) if p]
    return re.compile(r"(?<![a-z0-9])" + r"[-_\s]+".join(parts) + r"(?![a-z0-9])", re.IGNORECASE)


def find_matches(args: Any, off_limits: OffLimits) -> list[_Match]:
    """Every place in ``args`` that names a declared source as a URL or a search query."""
    patterns = [(s, source_pattern(s)) for s in off_limits.sources]
    found: list[_Match] = []
    for field, text in _strings(args):
        if field is not None and field.lower() in QUERY_KEYS:
            found += [_Match("query", field, s, text) for s, p in patterns if p.search(text)]
        for raw in _URL_RE.findall(text):
            url = raw.rstrip(".,;:)]}")
            decoded = unquote_plus(url)
            found += [_Match("url", field, s, url) for s, p in patterns if p.search(decoded)]
    return found


def _strings(value: Any, field: str | None = None) -> Iterator[tuple[str | None, str]]:
    """Every string in ``value`` with the name of the argument it sits under."""
    if isinstance(value, str):
        yield field, value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _strings(item, str(key))
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings(item, field)


class OffLimitsSourceRule(Rule):
    """R13: a call requested or searched for a source the contract's ``off_limits`` rules out."""

    id = "R13"
    finding_type = "off_limits_source"

    def coverage(self, trace: Trace, registry: ToolRegistry) -> Coverage | None:
        if registry.off_limits is None:
            return None
        calls = trace.tool_calls()
        checked = sum(1 for c in calls if c.args_unavailable is None)
        return Coverage(self.id, "tool calls", checked, len(calls))

    def run(self, trace: Trace, registry: ToolRegistry) -> list[Finding]:
        policy = registry.off_limits
        if policy is None:
            return []
        prompt = "\n".join(
            m.content
            for m in trace.steps
            if isinstance(m, Message) and m.role in (Role.USER, Role.SYSTEM)
        )
        findings: list[Finding] = []
        unknown: list[ToolCall] = []
        for call in trace.tool_calls():
            if call.args_unavailable is not None:
                unknown.append(call)
                continue
            matches = find_matches(call.args, policy)
            if matches:
                findings.append(self._finding(trace, registry, call, matches, policy, prompt))
        skipped = self.unknown_args_suppression(unknown, "off-limits sources")
        return findings + ([skipped] if skipped else [])

    def _finding(
        self,
        trace: Trace,
        registry: ToolRegistry,
        call: ToolCall,
        matches: list[_Match],
        policy: OffLimits,
        prompt: str,
    ) -> Finding:
        first = matches[0]
        result = trace.result_for(call)
        if result is None:
            outcome, said = "unrecorded", "no result was recorded"
        elif _failed(trace, result, registry):
            outcome, said = "failed", "it failed"
        else:
            outcome, said = "returned", "it returned a result"
        text = first.text if len(first.text) <= 120 else first.text[:117] + "..."
        verb = "requested" if first.surface == "url" else "searched for"
        evidence: dict[str, Any] = {
            "step_indices": [call.index] + ([result.index] if result is not None else []),
            "tool": call.name,
            "call_id": call.call_id,
            "field": first.field,
            "signal": first.surface,
            "source": first.source,
            "matched": first.text[:_TEXT_LIMIT],
            "outcome": outcome,
        }
        sources = list(dict.fromkeys(m.source for m in matches))
        if len(sources) > 1:
            evidence["sources"] = sources
        if policy.reason:
            evidence["reason"] = policy.reason
        given = first.surface == "url" and first.text in prompt
        if given:
            evidence["in_prompt"] = True
        return Finding(
            rule=self.id,
            finding_type=self.finding_type,
            tier=ConfidenceTier.HARD_DEFECT,
            summary=(
                f"{call.name!r} {verb} {text!r}, which names the off-limits source "
                f"{first.source!r} ({said})"
            ),
            evidence=evidence,
            possible_false_positive=given,
        )
