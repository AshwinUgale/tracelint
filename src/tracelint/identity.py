"""Stable, deterministic finding identity.

A finding needs an id that is the *same* every time tracelint sees the same defect in the same
run, and *different* for a different defect — independent of any output format. That id is what
makes write-back idempotent (re-running tracelint updates the existing Langfuse score / Phoenix
annotation instead of spraying duplicates) and what lets SARIF's ``partialFingerprints`` collapse
repeat runs into one code-scanning alert.

The id is a hash of the identity-bearing facts only — the rule, the finding kind, a *scope* (the
run/trace/artifact the finding belongs to), and stable per-step keys (a provider observation/span
id where the trace carries one, else the step index). Tier and human-readable summary are
deliberately excluded: they are how we *describe* a finding, not which finding it is.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass

from tracelint.findings import Finding


def finding_fingerprint(finding: Finding, *, scope: str, step_keys: Sequence[str]) -> str:
    """A short, stable hex id for ``finding`` within ``scope``.

    ``step_keys`` are the identity of the finding's evidence locations — provider observation/span
    ids when available (so the id survives re-parsing), otherwise the step indices as strings.
    """
    keys = ",".join(sorted(str(k) for k in step_keys))
    basis = f"{scope}|{finding.rule}|{finding.finding_type}|{keys}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]


# Evidence keys naming a tool a finding is about, in the order they read (R2b: failed -> consumer).
_TOOL_KEYS = ("tool", "errored_tool", "consumer")


@dataclass(frozen=True)
class FindingKey:
    """What a finding is about, independent of where it sits in a run.

    The fingerprint above pins a finding to its exact steps; this key keeps only what survives a
    re-run of the agent: the rule and kind, the tools involved, the argument fields (R3's field,
    the locations of R1's schema errors) and the signal (R2a/R2b). Step positions, values and run
    ids are left out, so a regenerated trace whose steps shift still yields the same key. A
    project's ignores match on it, and a CI baseline counts findings by it.
    """

    rule: str
    finding_type: str
    tools: tuple[str, ...] = ()
    fields: tuple[str, ...] = ()
    signal: str | None = None

    def describe(self) -> str:
        """A short label: ``R2b error_mishandled run_release_pipeline -> deploy``."""
        parts = [self.rule, self.finding_type]
        if self.tools:
            parts.append(" -> ".join(self.tools))
        if self.fields:
            parts.append("fields " + ", ".join(f or "(root)" for f in self.fields))
        if self.signal:
            parts.append(f"({self.signal})")
        return " ".join(parts)


def finding_key(finding: Finding) -> FindingKey:
    """The :class:`FindingKey` of ``finding``."""
    evidence = finding.evidence
    tools = [evidence[k] for k in _TOOL_KEYS if isinstance(evidence.get(k), str)]
    listed = evidence.get("tools")
    if isinstance(listed, list):
        tools += [t for t in listed if isinstance(t, str)]
    fields = set()
    if isinstance(evidence.get("field"), str):
        fields.add(evidence["field"])
    for error in evidence.get("errors") or []:  # R1: where the schema error sits, "" at the root
        if isinstance(error, dict) and isinstance(error.get("path"), str):
            fields.add(error["path"].lstrip("/"))
    signal = evidence.get("signal")
    return FindingKey(
        rule=finding.rule,
        finding_type=finding.finding_type,
        tools=tuple(dict.fromkeys(tools)),
        fields=tuple(sorted(fields)),
        signal=signal if isinstance(signal, str) else None,
    )
