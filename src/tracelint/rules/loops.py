"""R4 loop + R5 redundant call (spec §II.5, R4/R5; learning-doc 02 §4; deep-design Trap 4).

Both look across the *sequence* of tool calls, and both are **candidates** — a repeated call is not
proof of a bug (Trap 4: a retry-with-backoff is a loop, polling is repeated identical calls,
pagination is near-identical calls). They are flagged with evidence, never asserted.

**R4 — loop:** ``LOOP_THRESHOLD`` (3) consecutive calls with an identical signature
``(tool, normalized_args, result_class)`` and **no change in state**. A single retry (2 identical)
is normal and not flagged. A legitimate poll is excluded two ways: a tool declared ``polling`` in
its metadata is trusted, and a run in a waiting state that *eventually advances* to a different
``result_class`` is a progressing poll, not a stuck loop.

**R5 — redundant call:** a later call with the identical ``(tool, normalized_args)`` and the
**identical result** (fingerprint) as an earlier one, with real work in between (so it is not a
loop) and no side-effecting call between them (which could have changed the data, justifying a
re-fetch). Pagination differs by args and is not flagged; a refresh is legitimate → candidate.
Side-effect status is read from tool metadata, never guessed from a name; a tool *absent* from the
registry has an unverifiable side-effect status, so the finding still surfaces (the result is
byte-identical) but **discloses** the undeclared tool rather than silently assuming it inert.

A call whose arguments the trace did not record (redacted, positional, ...) never matches another
call — equality is unknowable — so where it could have formed a loop or a repeat, it is disclosed as
not checked instead of silently passing.
"""

from __future__ import annotations

from dataclasses import dataclass

from tracelint.findings import ConfidenceTier, Finding
from tracelint.rules.base import Rule
from tracelint.signatures import (
    call_args_key,
    is_waiting_class,
    result_class,
    result_fingerprint,
)
from tracelint.tools import ToolRegistry
from tracelint.trace import ToolCall, Trace

LOOP_THRESHOLD = 3


@dataclass
class _CallInfo:
    call: ToolCall
    args_key: str
    rclass: str
    result_fp: str
    fingerprint: str


def _analyze(trace: Trace) -> list[_CallInfo]:
    infos: list[_CallInfo] = []
    for call in trace.tool_calls():
        result = trace.result_for(call)
        args_key = call_args_key(call)
        result_fp = result_fingerprint(result)
        infos.append(
            _CallInfo(
                call=call,
                args_key=args_key,
                rclass=result_class(result),
                result_fp=result_fp,
                fingerprint=f"{call.name}|{args_key}|{result_fp}",
            )
        )
    return infos


class LoopRule(Rule):
    """R4: N consecutive identical no-progress calls (excluding legitimate polls)."""

    id = "R4"
    finding_type = "loop"

    def applicable(self, trace: Trace, registry: ToolRegistry) -> str | None:
        if len(trace.tool_calls()) < LOOP_THRESHOLD:
            return f"fewer than {LOOP_THRESHOLD} tool calls; no loop possible"
        return None

    def run(self, trace: Trace, registry: ToolRegistry) -> list[Finding]:
        infos = _analyze(trace)
        findings: list[Finding] = []
        i = 0
        n = len(infos)
        while i < n:
            j = i
            sig = (infos[i].call.name, infos[i].args_key, infos[i].rclass)
            while (
                j + 1 < n
                and (
                    infos[j + 1].call.name,
                    infos[j + 1].args_key,
                    infos[j + 1].rclass,
                )
                == sig
            ):
                j += 1
            run_len = j - i + 1
            if run_len >= LOOP_THRESHOLD and not self._is_legit_poll(infos, i, j, registry):
                findings.append(self._loop_finding(infos[i : j + 1]))
            i = j + 1
        disclosure = self.unknown_args_suppression(self._unknown_in_runs(infos, registry), "loops")
        if disclosure is not None:
            findings.append(disclosure)
        return findings

    def _unknown_in_runs(self, infos: list[_CallInfo], registry: ToolRegistry) -> list[ToolCall]:
        """Calls with unknown arguments inside a run of ``LOOP_THRESHOLD``+ consecutive calls to one
        (non-polling) tool in one result state — a loop R4 can neither confirm nor rule out."""
        unknown: list[ToolCall] = []
        i, n = 0, len(infos)
        while i < n:
            j = i
            head = (infos[i].call.name, infos[i].rclass)
            while j + 1 < n and (infos[j + 1].call.name, infos[j + 1].rclass) == head:
                j += 1
            meta = registry.metadata_for(head[0])
            if j - i + 1 >= LOOP_THRESHOLD and not (meta and meta.polling):
                unknown.extend(c.call for c in infos[i : j + 1] if c.call.args_unavailable)
            i = j + 1
        return unknown

    def _is_legit_poll(
        self, infos: list[_CallInfo], i: int, j: int, registry: ToolRegistry
    ) -> bool:
        head = infos[i]
        meta = registry.metadata_for(head.call.name)
        if meta and meta.polling:
            return True  # declared polling — trust the metadata (spec §II.5)
        if not is_waiting_class(head.rclass):
            return False
        # A waiting run that eventually advances (same call, different class later) is a real poll.
        for later in infos[j + 1 :]:
            if (
                later.call.name == head.call.name
                and later.args_key == head.args_key
                and later.rclass != head.rclass
            ):
                return True
        return False

    def _loop_finding(self, run: list[_CallInfo]) -> Finding:
        head = run[0]
        return Finding(
            rule=self.id,
            finding_type=self.finding_type,
            tier=ConfidenceTier.CANDIDATE,
            summary=(
                f"{head.call.name!r} called {len(run)} times in a row with identical arguments and "
                f"no change in result state ({head.rclass})"
            ),
            evidence={
                "step_indices": [c.call.index for c in run],
                "tool": head.call.name,
                "repeats": len(run),
                "result_class": head.rclass,
            },
            possible_false_positive=True,
        )


class RedundantCallRule(Rule):
    """R5: a non-consecutive identical call with the identical result and no mutation between."""

    id = "R5"
    finding_type = "redundant_call"

    def applicable(self, trace: Trace, registry: ToolRegistry) -> str | None:
        if len(trace.tool_calls()) < 2:
            return "fewer than 2 tool calls; no repetition possible"
        return None

    def run(self, trace: Trace, registry: ToolRegistry) -> list[Finding]:
        infos = _analyze(trace)
        findings: list[Finding] = []
        seen: dict[str, int] = {}  # fingerprint -> position of the earliest occurrence
        for pos, info in enumerate(infos):
            prev = seen.get(info.fingerprint)
            if prev is None:
                seen[info.fingerprint] = pos
                continue
            if pos - prev == 1:
                continue  # adjacent identical calls are loop territory (R4), not redundancy
            if self._mutating_between(infos, prev, pos, registry):
                continue  # a *declared* side-effecting call between may have changed the data
            # A tool absent from the registry could be a side effect we can't see. We still surface
            # the candidate (the result is byte-identical, so no mutation is the likely reading),
            # but disclose the unverified premise rather than silently assuming the tool is inert.
            undeclared = self._undeclared_between(infos, prev, pos, registry)
            findings.append(self._redundant_finding(infos[prev], info, undeclared))
            seen[info.fingerprint] = pos  # chain to the most recent occurrence
        disclosure = self.unknown_args_suppression(self._unknown_in_repeats(infos), "redundancy")
        if disclosure is not None:
            findings.append(disclosure)
        return findings

    def _unknown_in_repeats(self, infos: list[_CallInfo]) -> list[ToolCall]:
        """Calls with unknown arguments to a tool that returned the identical result more than
        once — a redundant repeat R5 can neither confirm nor rule out."""
        same_result: dict[tuple[str, str], list[ToolCall]] = {}
        for info in infos:
            same_result.setdefault((info.call.name, info.result_fp), []).append(info.call)
        return [
            call
            for calls in same_result.values()
            if len(calls) > 1
            for call in calls
            if call.args_unavailable is not None
        ]

    def _mutating_between(
        self, infos: list[_CallInfo], prev: int, pos: int, registry: ToolRegistry
    ) -> bool:
        for mid in infos[prev + 1 : pos]:
            meta = registry.metadata_for(mid.call.name)
            if meta and meta.side_effecting:
                return True
        return False

    def _undeclared_between(
        self, infos: list[_CallInfo], prev: int, pos: int, registry: ToolRegistry
    ) -> list[str]:
        """Names of in-between tools unknown to the registry — their side-effect status is
        unverifiable, so ``no mutation between`` cannot be asserted for them."""
        unknown = {
            mid.call.name
            for mid in infos[prev + 1 : pos]
            if registry.get(mid.call.name) is None
        }
        return sorted(unknown)

    def _redundant_finding(
        self, first: _CallInfo, again: _CallInfo, undeclared: list[str]
    ) -> Finding:
        note = (
            ""
            if not undeclared
            else (
                f" (unverified: undeclared tool(s) {', '.join(repr(u) for u in undeclared)} ran "
                "between — declare their side_effecting status to rule out a mutation)"
            )
        )
        return Finding(
            rule=self.id,
            finding_type=self.finding_type,
            tier=ConfidenceTier.CANDIDATE,
            summary=(
                f"{again.call.name!r} repeats an earlier identical call (same arguments and "
                f"result) with no mutating call in between" + note
            ),
            evidence={
                "step_indices": [first.call.index, again.call.index],
                "tool": again.call.name,
                "undeclared_between": undeclared,
            },
            possible_false_positive=True,
        )
