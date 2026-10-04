"""R9 — declared preconditions: a tool ran although a call it requires had failed or never ran.

R2b proves an agent acted on a failed result only when a value flows from the failure into the
action. Acting on a failed prerequisite *without* taking anything from it looks like any other
call: a lookup fails and the agent refunds the order the user named anyway, or a pipeline comes
back ``UNSTABLE`` and the agent deploys ``"latest"``. What tells that apart from a legitimate
action is a contract the trace doesn't carry, so the operator declares it on the tool, in the same
``tools.json`` as ``side_effecting`` and ``failure_when``::

    "refund_order": {"metadata": {"side_effecting": true,
                                  "requires": [{"tool": "get_order", "same": ["order_id"]}]}}

Before ``refund_order`` runs, the **latest** ``get_order`` for the same ``order_id`` must have
returned successfully. The latest call decides, so a retry that succeeds satisfies the requirement
and a later failure un-satisfies it; ``same`` scopes it to one entity, so refunding order B needs
B's lookup, not A's. A required call counts only once its result is back before the action: firing
both in parallel, without waiting, is not checking first. ``"succeeded": false`` asks only that the
call returned, whatever its outcome.

A violation is a ``hard_defect``: the operator declared the requirement, so it is a fact, not a
guess. Success is read the way R2a reads it: a structured error or a matching ``failure_when`` is a
failure, a ``failure_when`` that resolves to no match or an explicit OK is a success. Anything else
(no status, no declared ``failure_when``) can't be verified, so it is disclosed as not checked, with
the fix named, never passed. So is a check whose arguments the trace did not record. Tools that
declare no ``requires`` are not checked at all, and a trace without any adds nothing to the report.
"""

from __future__ import annotations

from bisect import bisect_left
from typing import Any

from tracelint.findings import (
    SUPPRESS_NEEDS_CONTRACT,
    SUPPRESS_NOT_RECORDED,
    ConfidenceTier,
    Coverage,
    Finding,
)
from tracelint.predicates import PredicateResult
from tracelint.rules.base import Rule
from tracelint.signatures import is_structured_error
from tracelint.tools import Requirement, ToolRegistry
from tracelint.trace import ResultStatus, ToolCall, ToolResult, Trace
from tracelint.valueutil import normalize

FAILED = "prerequisite_failed"
MISSING = "prerequisite_missing"


class PreconditionRule(Rule):
    """R9: a tool that declares ``requires`` ran without its required call succeeding first."""

    id = "R9"
    finding_type = "unmet_precondition"

    def __init__(self) -> None:
        self._memo: tuple[Trace, ToolRegistry, list[_Check]] | None = None

    def coverage(self, trace: Trace, registry: ToolRegistry) -> Coverage | None:
        checks = self._checks(trace, registry)
        if not checks:
            return None  # nothing declares a precondition: nothing to report
        decided: dict[int, bool] = {}  # per call: were all of its requirements decided?
        for check in checks:
            known = check.outcome not in ("unknown", "args_unknown")
            decided[check.call.index] = decided.get(check.call.index, True) and known
        return Coverage(
            self.id, "calls with declared preconditions", sum(decided.values()), len(decided)
        )

    def run(self, trace: Trace, registry: ToolRegistry) -> list[Finding]:
        findings: list[Finding] = []
        unchecked: list[ToolCall] = []
        for check in self._checks(trace, registry):
            if check.outcome == "args_unknown":
                unchecked.append(check.call)
            elif check.outcome == "unknown":
                findings.append(self._unverified(check))
            elif check.outcome in (FAILED, MISSING):
                findings.append(self._violation(check))
        disclosure = self.unknown_args_suppression(unchecked, "declared preconditions")
        if disclosure is not None:
            findings.append(disclosure)
        return findings

    def _checks(self, trace: Trace, registry: ToolRegistry) -> list[_Check]:
        """Every requirement of every call, decided once per trace (coverage and run share it)."""
        if self._memo is not None and self._memo[0] is trace and self._memo[1] is registry:
            return self._memo[2]
        history: History | None = None
        lookups: dict[Requirement, _Lookup] = {}
        checks: list[_Check] = []
        for call in trace.tool_calls():
            for requirement in _requirements(registry, call):
                if history is None:
                    history = _history(trace)
                if requirement not in lookups:
                    lookups[requirement] = _Lookup(history, requirement)
                checks.append(_Check(lookups[requirement], registry, call, requirement))
        self._memo = (trace, registry, checks)
        return checks

    def _violation(self, check: _Check) -> Finding:
        call, req = check.call, check.requirement
        scope = check.scope()
        if check.outcome == FAILED:
            assert check.prerequisite is not None and check.result is not None
            what = f"after the required {req.tool!r}{scope} failed ({check.failure_detail()})"
            steps = [check.result.index, call.index]
        else:
            first = f"a successful {req.tool!r}" if req.succeeded else f"a {req.tool!r} call"
            what = f"without {first}{scope} first"
            steps = [call.index]
        evidence: dict[str, Any] = {
            "step_indices": steps,
            "tool": call.name,
            "requires": req.tool,
            "signal": check.outcome,
        }
        if req.same:
            evidence["same"] = {name: call.args.get(name) for name in req.same}
        if check.prerequisite is not None:
            evidence["prerequisite_step"] = check.prerequisite.index
        return Finding(
            rule=self.id,
            finding_type=self.finding_type,
            tier=ConfidenceTier.HARD_DEFECT,
            summary=f"{call.name!r} ran {what}",
            evidence=evidence,
        )

    def _unverified(self, check: _Check) -> Finding:
        call, req = check.call, check.requirement
        if check.reason:
            reason = check.reason
        else:
            reason = (
                f"cannot verify the required {req.tool!r} succeeded before {call.name!r}: its "
                f"result shows no error and {req.tool!r} declares no failure_when to read it — "
                f"declare one"
            )
        category = SUPPRESS_NOT_RECORDED if check.reason else SUPPRESS_NEEDS_CONTRACT
        steps = [check.result.index, call.index] if check.result else [call.index]
        return Finding(
            rule=self.id,
            finding_type=self.finding_type,
            tier=ConfidenceTier.CANDIDATE,
            summary=f"rule {self.id} suppressed for {call.name!r}: {reason}",
            evidence={"step_indices": steps, "tool": call.name, "requires": req.tool},
            suppressed_reason=reason,
            suppressed_category=category,
        )


def _requirements(registry: ToolRegistry, call: ToolCall) -> tuple[Requirement, ...]:
    meta = registry.metadata_for(call.name)
    return meta.requires if meta else ()


History = dict[str, list[tuple[ToolCall, ToolResult | None]]]


def _history(trace: Trace) -> History:
    """Each tool's calls in order, with their results, built in one pass with the same pairing as
    :meth:`Trace.result_for` (the first later result sharing the call's id)."""
    history: History = {}
    waiting: dict[str, list[tuple[str, int]]] = {}  # call_id -> (tool, position) awaiting a result
    for step in trace.steps:
        if isinstance(step, ToolCall):
            calls = history.setdefault(step.name, [])
            waiting.setdefault(step.call_id, []).append((step.name, len(calls)))
            calls.append((step, None))
        elif isinstance(step, ToolResult):
            for name, position in waiting.pop(step.call_id, []):
                call, _ = history[name][position]
                history[name][position] = (call, step)
    return history


class _Lookup:
    """The calls of one required tool that returned, searchable by when each result came back
    and, for a requirement with ``same``, by those arguments' values (one entity's calls)."""

    def __init__(self, history: History, requirement: Requirement) -> None:
        self._calls: dict[tuple[str, ...], list[tuple[int, ToolCall, ToolResult]]] = {}
        hidden: list[int] = []
        for call, result in history.get(requirement.tool, []):
            if result is None:
                continue  # never returned
            if requirement.same and call.args_unavailable is not None:
                hidden.append(result.index)
                continue
            key = _entity(call, requirement.same)
            if key is not None:
                self._calls.setdefault(key, []).append((result.index, call, result))
        for entries in self._calls.values():
            entries.sort(key=lambda entry: entry[0])
        self._returned_at = {key: [e[0] for e in entries] for key, entries in self._calls.items()}
        self._hidden = sorted(hidden)

    def latest(self, key: tuple[str, ...], before: int) -> tuple[ToolCall, ToolResult] | None:
        """The call for ``key`` whose result came back last before step ``before``."""
        returned_at = self._returned_at.get(key)
        i = bisect_left(returned_at, before) if returned_at else 0
        if not i:
            return None
        _, call, result = self._calls[key][i - 1]
        return call, result

    def hidden_before(self, before: int) -> int:
        """How many calls with unrecorded arguments returned before step ``before``."""
        return bisect_left(self._hidden, before)


def _entity(call: ToolCall, names: tuple[str, ...]) -> tuple[str, ...] | None:
    """The normalized values of ``names`` in ``call``'s arguments, or ``None`` if one is missing."""
    values = [call.args.get(name) for name in names]
    if any(value is None for value in values):
        return None
    return tuple(normalize(value) for value in values)


class _Check:
    """One requirement of one call, decided: ``outcome`` is ``"ok"``, a violation (``FAILED`` /
    ``MISSING``), ``"unknown"`` (can't verify; ``reason`` may say why) or ``"args_unknown"``."""

    def __init__(
        self, lookup: _Lookup, registry: ToolRegistry, call: ToolCall, requirement: Requirement
    ) -> None:
        self.call = call
        self.requirement = requirement
        self.registry = registry
        self.prerequisite: ToolCall | None = None
        self.result: ToolResult | None = None
        self.reason: str | None = None
        self.outcome = self._decide(lookup)

    def _decide(self, lookup: _Lookup) -> str:
        req, call = self.requirement, self.call
        key = _entity(call, req.same) if call.args_unavailable is None else None
        if req.same and key is None:
            return "args_unknown"  # can't tell which entity this call is about
        hidden = lookup.hidden_before(call.index)
        if hidden:
            # One of the unrecorded calls may be the latest for this entity.
            self.reason = (
                f"{hidden} call(s) to {req.tool!r} before {call.name!r} have unknown arguments, so "
                f"the latest one{self.scope()} can't be identified"
            )
            return "unknown"
        found = lookup.latest(key or (), call.index)  # a call still in flight hasn't returned
        if found is None:
            return MISSING
        self.prerequisite, self.result = found
        if not req.succeeded:
            return "ok"
        return self._result_outcome()

    def _result_outcome(self) -> str:
        assert self.result is not None and self.prerequisite is not None
        if is_structured_error(self.result):
            return FAILED
        meta = self.registry.metadata_for(self.requirement.tool)
        predicate = meta.failure_when if meta else None
        if predicate is not None:
            verdict = predicate.evaluate(self.result.content)
            if verdict is PredicateResult.MATCH:
                return FAILED
            if verdict is PredicateResult.NO_MATCH:
                return "ok"
            self.reason = (
                f"cannot verify the required {self.requirement.tool!r} succeeded before "
                f"{self.call.name!r}: the field its failure_when reads "
                f"({predicate.pointer or '(result)'}) is absent"
            )
            return "unknown"
        return "ok" if self.result.status is ResultStatus.OK else "unknown"

    def failure_detail(self) -> str:
        assert self.result is not None
        meta = self.registry.metadata_for(self.requirement.tool)
        predicate = meta.failure_when if meta else None
        if (
            predicate is not None
            and predicate.evaluate(self.result.content) is PredicateResult.MATCH
        ):
            return predicate.describe(self.result.content)
        if self.result.http_status is not None:
            return f"http {self.result.http_status}"
        return str(self.result.error or "status=error")

    def scope(self) -> str:
        same = self.requirement.same
        if not same:
            return ""
        return " for " + ", ".join(f"{n}={self.call.args.get(n)!r}" for n in same)
