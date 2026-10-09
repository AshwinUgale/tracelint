"""SARIF 2.1.0 output for GitHub code scanning (issue #13).

GitHub's code-scanning ingest speaks **SARIF** (Static Analysis Results Interchange Format). Emit
it and tracelint's findings become first-class alerts: they show in the repository's *Security →
Code scanning* tab and as inline annotations on the pull request that introduced the trace, with
no bespoke glue on the user's side beyond the standard ``upload-sarif`` step.

The mapping keeps tracelint's tiers honest inside SARIF's ``level`` vocabulary:

- ``hard_defect`` -> ``error``   (structurally-provable; the tier that fails CI)
- ``hard_event``  -> ``warning`` (a certain fact — a tool errored, a side effect repeated)
- ``candidate``   -> ``note``    (a heuristic shown for review)

Suppressions are *not* results (they record what could not be checked, not a defect) and are
omitted. A trace file is the "artifact" a finding is located in. A trace step is not a source line,
so a result's ``region.startLine`` is a best-effort anchor: the line of the trace file where the
finding's *first* step can be found by its source token — an OTel/Phoenix ``span_id``, a Langfuse
``observation_id``, or a ``call_id`` — falling back to line 1 when the step can't be located (a
merged span from another file, a token the format doesn't serialise). The exact ``step_indices``
always travel in ``properties`` for the reader. ``partialFingerprints`` give GitHub a stable
identity so the same finding across runs is one alert, not a new one each time.
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Mapping, Sequence
from typing import Any

from tracelint.findings import ConfidenceTier, Finding, LintReport
from tracelint.identity import finding_fingerprint
from tracelint.trace import Trace

SARIF_VERSION = "2.1.0"
SCHEMA_URI = "https://json.schemastore.org/sarif-2.1.0.json"
INFORMATION_URI = "https://github.com/AshwinUgale/tracelint"
#: Per-rule help pages (docs/rules.md, one anchor per rule). Each rule descriptor's ``helpUri``
#: points at its anchor, so a GitHub code-scanning alert links straight to what the rule means and
#: how to resolve it, rather than to the repository root.
HELP_URI = INFORMATION_URI + "/blob/main/docs/rules.md"

_LEVEL: dict[ConfidenceTier, str] = {
    ConfidenceTier.HARD_DEFECT: "error",
    ConfidenceTier.HARD_EVENT: "warning",
    ConfidenceTier.CANDIDATE: "note",
}

# Presentation metadata for the SARIF rule catalogue (tool.driver.rules). ``level`` is the rule's
# default configuration; the per-result level (driven by the finding's actual tier) is always set
# and takes precedence, since one finding_type can surface at more than one tier.
_RULE_META: dict[str, dict[str, str]] = {
    "R1": {
        "name": "SchemaViolation",
        "text": "A tool call's arguments don't satisfy the tool's declared JSON Schema.",
        "level": "error",
    },
    "R2a": {
        "name": "ToolError",
        "text": "A tool returned a structured error (e.g. an HTTP status >= 400).",
        "level": "warning",
    },
    "R2b": {
        "name": "ErrorConsumed",
        "text": "A value from an errored result is reused by a later side-effecting call.",
        "level": "error",
    },
    "R3": {
        "name": "HallucinatedArgument",
        "text": "An argument value isn't derivable from anything the agent observed in the trace.",
        "level": "note",
    },
    "R4": {
        "name": "Loop",
        "text": "The same call repeats with no change in state (retries and polls excluded).",
        "level": "note",
    },
    "R5": {
        "name": "RedundantCall",
        "text": "An identical call and result with no state change in between.",
        "level": "note",
    },
    "R6": {
        "name": "MalformedArguments",
        "text": "The emitted tool-call arguments aren't well-formed against the call contract.",
        "level": "error",
    },
    "R7": {
        "name": "UnknownTool",
        "text": "A tool was called that isn't in the declared toolset; behavior is unverified.",
        "level": "note",
    },
    "R8": {
        "name": "DuplicateSideEffect",
        "text": (
            "A non-idempotent side-effecting call repeated with equivalent arguments after the "
            "first succeeded."
        ),
        "level": "warning",
    },
    "R9": {
        "name": "UnmetPrecondition",
        "text": (
            "A tool ran although a call its contract requires had failed, or had not returned, "
            "first."
        ),
        "level": "error",
    },
    "R10": {
        "name": "ResultContractViolation",
        "text": "A tool's result does not satisfy the output JSON Schema its contract declares.",
        "level": "warning",
    },
    "R11": {
        "name": "ContractDrift",
        "text": "A tool's schema in the trace differs from the committed tools.json contract.",
        "level": "warning",
    },
    "R12": {
        "name": "UnresolvedSideEffect",
        "text": (
            "A side-effecting call whose outcome the trace never resolved (no result, or a "
            "failure nothing recovered)."
        ),
        "level": "note",
    },
    "R13": {
        "name": "OffLimitsSource",
        "text": "A call requested or searched for a source the contract declares off-limits.",
        "level": "error",
    },
}


def _fingerprint(uri: str, finding: Finding) -> str:
    """A stable identity for a finding so GitHub treats it as one alert across runs."""
    return finding_fingerprint(
        finding, scope=uri, step_keys=[str(i) for i in finding.step_indices]
    )


def _help_uri(rule_id: str) -> str:
    """The rule's help anchor in docs/rules.md, or the page itself for an unknown rule.

    Anchors are the lowercased rule id (``R2b`` -> ``#r2b``), matching the ``<a id=...>`` tags in
    docs/rules.md. An id with no descriptor (e.g. an external rule) links to the page, not a
    dangling anchor.
    """
    if rule_id in _RULE_META:
        return f"{HELP_URI}#{rule_id.lower()}"
    return HELP_URI


def _rule_descriptor(rule_id: str, finding_type: str) -> dict[str, Any]:
    meta = _RULE_META.get(rule_id)
    name = meta["name"] if meta else finding_type
    text = meta["text"] if meta else f"tracelint rule {rule_id} ({finding_type})."
    level = meta["level"] if meta else "warning"
    return {
        "id": rule_id,
        "name": name,
        "shortDescription": {"text": text},
        "fullDescription": {"text": text},
        "helpUri": _help_uri(rule_id),
        "defaultConfiguration": {"level": level},
        "properties": {"tags": ["agent-trace", "tracelint"]},
    }


def _line_starts(text: str) -> list[int]:
    """Char offsets at which each line begins (so an offset maps to a 1-based line by bisect)."""
    starts = [0]
    start = text.find("\n")
    while start != -1:
        starts.append(start + 1)
        start = text.find("\n", start + 1)
    return starts


def _locate(token: str, text: str, starts: list[int]) -> int | None:
    """The 1-based line of ``token``'s first occurrence in ``text``, or None if absent.

    The quoted form is tried first (tokens are JSON string values, e.g. ``"span_id": "abc"``), so a
    token is not matched inside a longer value; a raw search is the fallback.
    """
    for needle in (f'"{token}"', token):
        pos = text.find(needle)
        if pos != -1:
            return bisect_right(starts, pos)
    return None


def _step_token(step: Any, *, allow_call_id: bool) -> str | None:
    """A distinctive string that locates ``step`` in its source file: a globally-unique span /
    observation id when the adapter recorded one, else the ``call_id`` (only when unambiguous —
    ``call_id`` repeats across the runs of a multi-trace file, so the caller disables it there)."""
    source = getattr(step, "source", None)
    if source is not None:
        if source.span_id:
            return str(source.span_id)
        if source.observation_id:
            return str(source.observation_id)
    if allow_call_id:
        call_id = getattr(step, "call_id", None)
        if call_id:
            return str(call_id)
    return None


def build_line_map(
    trace: Trace, text: str, indices: set[int], *, allow_call_id: bool = True
) -> dict[int, int]:
    """Map each wanted step index to its 1-based line in ``text`` (the trace file), best-effort.

    Only ``indices`` (the steps findings actually anchor to) are located, so cost stays tied to the
    number of findings, not the trace length. A step with no token, or a token not in ``text`` (a
    span merged in from another file), is simply absent from the map — the caller then leaves that
    finding at line 1.
    """
    if not indices:
        return {}
    starts = _line_starts(text)
    out: dict[int, int] = {}
    for idx in indices:
        if not 0 <= idx < len(trace.steps):
            continue
        token = _step_token(trace.steps[idx], allow_call_id=allow_call_id)
        if not token:
            continue
        line = _locate(token, text, starts)
        if line is not None:
            out[idx] = line
    return out


def _result(uri: str, finding: Finding, rule_index: int, start_line: int = 1) -> dict[str, Any]:
    message = finding.summary or f"{finding.rule} {finding.finding_type}"
    props: dict[str, Any] = {
        "tier": finding.tier.value,
        "finding_type": finding.finding_type,
    }
    if finding.step_indices:
        props["step_indices"] = finding.step_indices
    if finding.possible_false_positive:
        props["possible_false_positive"] = True
    return {
        "ruleId": finding.rule,
        "ruleIndex": rule_index,
        "level": _LEVEL[finding.tier],
        "message": {"text": message},
        "locations": [
            {
                "physicalLocation": {
                    "artifactLocation": {"uri": uri},
                    "region": {"startLine": start_line},
                }
            }
        ],
        "partialFingerprints": {"tracelintFinding/v1": _fingerprint(uri, finding)},
        "properties": props,
    }


def to_sarif(
    reports: Sequence[LintReport],
    *,
    tool_version: str,
    uris: Sequence[str] | None = None,
    line_maps: Sequence[Mapping[int, int]] | None = None,
) -> dict[str, Any]:
    """Render lint reports as a SARIF 2.1.0 log for GitHub code scanning.

    ``uris`` optionally gives the source file each report was linted from (same length and order as
    ``reports``); a finding is located in that file. When omitted, a report's ``run_id`` is used as
    the artifact URI. ``line_maps`` optionally gives, per report, a ``{step_index: 1-based line}``
    map (see :func:`build_line_map`): a finding's ``region.startLine`` is the line of its first
    step, or 1 when unmapped. Only active findings become results — suppressions are excluded.
    """
    if uris is not None and len(uris) != len(reports):
        raise ValueError("uris must have the same length as reports")
    if line_maps is not None and len(line_maps) != len(reports):
        raise ValueError("line_maps must have the same length as reports")

    results: list[dict[str, Any]] = []
    referenced: list[str] = []  # rule ids in first-seen order -> rules[] and ruleIndex
    type_for: dict[str, str] = {}
    for i, report in enumerate(reports):
        uri = uris[i] if uris is not None else report.run_id
        line_map = line_maps[i] if line_maps is not None else None
        for finding in report.active_findings:
            if finding.rule not in referenced:
                referenced.append(finding.rule)
                type_for[finding.rule] = finding.finding_type
            start_line = 1
            if line_map and finding.step_indices:
                start_line = line_map.get(finding.step_indices[0], 1)
            results.append(_result(uri, finding, referenced.index(finding.rule), start_line))

    rules = [_rule_descriptor(rid, type_for[rid]) for rid in referenced]
    has_defect = any(r.has_hard_defect for r in reports)
    return {
        "$schema": SCHEMA_URI,
        "version": SARIF_VERSION,
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "tracelint",
                        "informationUri": INFORMATION_URI,
                        "version": tool_version,
                        "rules": rules,
                    }
                },
                "invocations": [{"executionSuccessful": not has_defect}],
                "results": results,
            }
        ],
    }
