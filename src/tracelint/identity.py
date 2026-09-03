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

from tracelint.findings import Finding


def finding_fingerprint(finding: Finding, *, scope: str, step_keys: Sequence[str]) -> str:
    """A short, stable hex id for ``finding`` within ``scope``.

    ``step_keys`` are the identity of the finding's evidence locations — provider observation/span
    ids when available (so the id survives re-parsing), otherwise the step indices as strings.
    """
    keys = ",".join(sorted(str(k) for k in step_keys))
    basis = f"{scope}|{finding.rule}|{finding.finding_type}|{keys}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]
