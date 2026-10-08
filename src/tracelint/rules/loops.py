"""R4 loop + R5 redundant call (spec §II.5, R4/R5; learning-doc 02 §4; deep-design Trap 4).

Both look across the *sequence* of tool calls, and both are **candidates** — a repeated call is not
proof of a bug (Trap 4: a retry-with-backoff is a loop, polling is repeated identical calls,
pagination is near-identical calls). They are flagged with evidence, never asserted.

**R4 — loop:** ``LOOP_THRESHOLD`` (3) consecutive calls to the same tool with the same arguments
that returned **the identical result** each time: the agent repeated itself and learned nothing. A
single retry (2 identical) is normal and not flagged. A repeat whose output changed is progress, not
a loop — a growing build log, a training run's progress — so outputs are compared exactly, not by a
coarse "ok" class (on 10,541 real agent runs, that coarse comparison made 55% of R4's loops output
that was progressing). Polling is excluded three ways: a tool declared ``polling`` in its metadata
is trusted; a run in a waiting state (``status: pending``) that *eventually advances* is a
progressing poll; and a call that waits on something running (:func:`is_poll_call` — empty input,
``sleep``, a tool named for waiting) repeats by design, so it is flagged only when the trace ended
while it was still waiting with nothing new.

**R5 — redundant call:** a later call with the identical ``(tool, normalized_args)`` and the
**identical result** (fingerprint) as an earlier one, with real work in between (so it is not a
loop) and no side-effecting call between them (which could have changed the data, justifying a
re-fetch). Pagination differs by args and is not flagged; a refresh is legitimate → candidate.
Side-effect status is read from tool metadata, never guessed from a name; a tool *absent* from the
registry has an unverifiable side-effect status, so the finding still surfaces (the result is
byte-identical) but **discloses** the undeclared tool rather than silently assuming it inert.

A call whose arguments the trace did not record (redacted, positional, ...), or whose result it did
not record (an earlier call in a batch that returns one observation, a server-side tool), never
matches another call — equality is unknowable — so where it could have formed a loop or a repeat, it
is disclosed as not checked instead of silently passing or being asserted.
"""

from __future__ import annotations

from dataclasses import dataclass

from tracelint.findings import ConfidenceTier, Finding
from tracelint.rules.base import Rule
from tracelint.signatures import (
    call_args_key,
    is_poll_call,
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
    #: The exact result fingerprint, or ``None`` when no result was recorded (unknown).
    result_fp: str | None
    #: The call waits on something already running (:func:`is_poll_call`).
    poll: bool

    @property
    def signature(self) -> str:
        """What a repeat must match: the tool, its arguments, and its exact result. A call with no
        recorded result matches nothing — its result is unknown, not equal to another's."""
        if self.result_fp is None:
            return f"\x00no-result:{self.call.index}:{self.call.call_id}"
        return f"{self.call.name}|{self.args_key}|{self.result_fp}"


def _analyze(trace: Trace) -> list[_CallInfo]:
    infos: list[_CallInfo] = []
    for call in trace.tool_calls():
        result = trace.result_for(call)
        infos.append(
            _CallInfo(
                call=call,
                args_key=call_args_key(call),
                rclass=result_class(result),
                result_fp=result_fingerprint(result) if result is not None else None,
                poll=is_poll_call(call),
            )
        )
    return infos


def _same_call_runs(infos: list[_CallInfo]) -> list[tuple[int, int]]:
    """``(start, end)`` of each maximal run of consecutive calls to one tool with one argument set,
    whatever they returned."""
    runs: list[tuple[int, int]] = []
    i, n = 0, len(infos)
    while i < n:
        j = i
        key = (infos[i].call.name, infos[i].args_key)
        while j + 1 < n and (infos[j + 1].call.name, infos[j + 1].args_key) == key:
            j += 1
        runs.append((i, j))
        i = j + 1
    return runs


class LoopRule(Rule):
    """R4: N consecutive identical calls with the identical result (excluding legitimate polls)."""

    id = "R4"
    finding_type = "loop"

    def applicable(self, trace: Trace, registry: ToolRegistry) -> str | None:
        if len(trace.tool_calls()) < LOOP_THRESHOLD:
            return self.not_applicable(f"fewer than {LOOP_THRESHOLD} tool calls; no loop possible")
        return None

    def run(self, trace: Trace, registry: ToolRegistry) -> list[Finding]:
        infos = _analyze(trace)
        findings: list[Finding] = []
        i, n = 0, len(infos)
        while i < n:
            j = i
            while j + 1 < n and infos[j + 1].signature == infos[i].signature:
                j += 1
            if j - i + 1 >= LOOP_THRESHOLD and not self._is_legit_poll(infos, i, j, registry):
                findings.append(self._loop_finding(infos[i : j + 1]))
            i = j + 1
        for disclosure in (
            self.unknown_args_suppression(self._unknown_in_runs(infos, registry), "loops"),
            self.unrecorded_result_suppression(self._unrecorded_in_runs(infos, registry), "loops"),
        ):
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

    def _unrecorded_in_runs(
        self, infos: list[_CallInfo], registry: ToolRegistry
    ) -> list[ToolCall]:
        """Calls with no recorded result inside a run of ``LOOP_THRESHOLD``+ consecutive identical
        calls whose recorded results (if any) agree — a loop R4 can neither confirm nor rule out."""
        missing: list[ToolCall] = []
        for i, j in _same_call_runs(infos):
            run = infos[i : j + 1]
            unrecorded = [c.call for c in run if c.result_fp is None]
            recorded = {c.result_fp for c in run if c.result_fp is not None}
            meta = registry.metadata_for(run[0].call.name)
            excused = bool(meta and meta.polling) or (run[0].poll and j != len(infos) - 1)
            if len(run) >= LOOP_THRESHOLD and unrecorded and len(recorded) <= 1 and not excused:
                missing.extend(unrecorded)
        return missing

    def _is_legit_poll(
        self, infos: list[_CallInfo], i: int, j: int, registry: ToolRegistry
    ) -> bool:
        head = infos[i]
        meta = registry.metadata_for(head.call.name)
        if meta and meta.polling:
            return True  # declared polling — trust the metadata (spec §II.5)
        if is_waiting_class(head.rclass):
            # A waiting run that eventually advances (same call, different state later) is a poll.
            return any(
                later.call.name == head.call.name
                and later.args_key == head.args_key
                and later.rclass != head.rclass
                for later in infos[j + 1 :]
            )
        if head.poll:
            # A call that waits on something running repeats by design: it is stuck only if the
            # trace ended while it was still waiting with nothing new.
            return j != len(infos) - 1
        return False

    def _loop_finding(self, run: list[_CallInfo]) -> Finding:
        head = run[0]
        if head.poll:
            summary = (
                f"{head.call.name!r} polled {len(run)} times in a row with an unchanged result, "
                "and the trace ended still waiting"
            )
        else:
            summary = (
                f"{head.call.name!r} called {len(run)} times in a row with identical arguments "
                "and an identical result"
            )
        return Finding(
            rule=self.id,
            finding_type=self.finding_type,
            tier=ConfidenceTier.CANDIDATE,
            summary=summary,
            evidence={
                "step_indices": [c.call.index for c in run],
                "tool": head.call.name,
                "repeats": len(run),
                "result_class": head.rclass,
                "poll": head.poll,
            },
            possible_false_positive=True,
        )


class RedundantCallRule(Rule):
    """R5: a non-consecutive identical call with the identical result and no mutation between."""

    id = "R5"
    finding_type = "redundant_call"

    def applicable(self, trace: Trace, registry: ToolRegistry) -> str | None:
        if len(trace.tool_calls()) < 2:
            return self.not_applicable("fewer than 2 tool calls; no repetition possible")
        return None

    def run(self, trace: Trace, registry: ToolRegistry) -> list[Finding]:
        infos = _analyze(trace)
        findings: list[Finding] = []
        seen: dict[str, int] = {}  # signature -> position of the earliest occurrence
        for pos, info in enumerate(infos):
            prev = seen.get(info.signature)
            if prev is None:
                seen[info.signature] = pos
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
            seen[info.signature] = pos  # chain to the most recent occurrence
        for disclosure in (
            self.unknown_args_suppression(self._unknown_in_repeats(infos), "redundancy"),
            self.unrecorded_result_suppression(self._unrecorded_in_repeats(infos), "redundancy"),
        ):
            if disclosure is not None:
                findings.append(disclosure)
        return findings

    def _unknown_in_repeats(self, infos: list[_CallInfo]) -> list[ToolCall]:
        """Calls with unknown arguments to a tool that returned the identical result more than
        once — a redundant repeat R5 can neither confirm nor rule out."""
        same_result: dict[tuple[str, str], list[ToolCall]] = {}
        for info in infos:
            if info.result_fp is not None:
                same_result.setdefault((info.call.name, info.result_fp), []).append(info.call)
        return [
            call
            for calls in same_result.values()
            if len(calls) > 1
            for call in calls
            if call.args_unavailable is not None
        ]

    def _unrecorded_in_repeats(self, infos: list[_CallInfo]) -> list[ToolCall]:
        """Calls with no recorded result that repeat a non-adjacent identical call whose recorded
        results (if any) agree — a redundant repeat R5 can neither confirm nor rule out."""
        by_call: dict[tuple[str, str], list[_CallInfo]] = {}
        for info in infos:
            by_call.setdefault((info.call.name, info.args_key), []).append(info)
        positions = {id(info): pos for pos, info in enumerate(infos)}
        missing: list[ToolCall] = []
        for group in by_call.values():
            spots = [positions[id(info)] for info in group]
            spread = len(group) > 1 and max(spots) - min(spots) > 1  # not just adjacent repeats
            recorded = {info.result_fp for info in group if info.result_fp is not None}
            if spread and len(recorded) <= 1:
                missing.extend(info.call for info in group if info.result_fp is None)
        return missing

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
