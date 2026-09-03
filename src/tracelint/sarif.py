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
omitted. A trace file is the "artifact" a finding is located in; because a trace step is not a
source line, results anchor at the trace file (line 1) and carry the exact ``step_indices`` in
``properties`` for the reader. ``partialFingerprints`` give GitHub a stable identity so the same
finding across runs is one alert, not a new one each time.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from typing import Any

from tracelint.findings import ConfidenceTier, Finding, LintReport

SARIF_VERSION = "2.1.0"
SCHEMA_URI = "https://json.schemastore.org/sarif-2.1.0.json"
INFORMATION_URI = "https://github.com/AshwinUgale/tracelint"

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
}


def _fingerprint(uri: str, finding: Finding) -> str:
    """A stable identity for a finding so GitHub treats it as one alert across runs."""
    steps = ",".join(str(i) for i in sorted(finding.step_indices))
    basis = f"{uri}|{finding.rule}|{finding.finding_type}|{finding.tier.value}|{steps}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]


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
        "helpUri": INFORMATION_URI,
        "defaultConfiguration": {"level": level},
        "properties": {"tags": ["agent-trace", "tracelint"]},
    }


def _result(uri: str, finding: Finding, rule_index: int) -> dict[str, Any]:
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
                    "region": {"startLine": 1},
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
) -> dict[str, Any]:
    """Render lint reports as a SARIF 2.1.0 log for GitHub code scanning.

    ``uris`` optionally gives the source file each report was linted from (same length and order as
    ``reports``); a finding is located in that file. When omitted, a report's ``run_id`` is used as
    the artifact URI. Only active findings become results — suppressions are excluded by design.
    """
    if uris is not None and len(uris) != len(reports):
        raise ValueError("uris must have the same length as reports")

    results: list[dict[str, Any]] = []
    referenced: list[str] = []  # rule ids in first-seen order -> rules[] and ruleIndex
    type_for: dict[str, str] = {}
    for i, report in enumerate(reports):
        uri = uris[i] if uris is not None else report.run_id
        for finding in report.active_findings:
            if finding.rule not in referenced:
                referenced.append(finding.rule)
                type_for[finding.rule] = finding.finding_type
            results.append(_result(uri, finding, referenced.index(finding.rule)))

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
