"""Observability-platform integrations.

An **adapter** (``tracelint.adapters``) answers one question: *how do I normalize this provider's
payload into a canonical Trace?* It is pure and dependency-free.

An **integration** answers a different one: *how does tracelint behave as a citizen of this
platform?* — fetch the traces the user already collects, run the deterministic checks, and write
the verdict back into the platform's own model (Langfuse Scores, Phoenix annotations) so the user
never has to leave the tool they already debug in. Integrations may depend on a vendor SDK and are
opt-in extras; the rule engine and adapters stay clean.
"""

from __future__ import annotations

from tracelint.integrations.base import ScorePlan, plan_scores

__all__ = ["ScorePlan", "plan_scores"]
