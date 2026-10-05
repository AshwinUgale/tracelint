"""docs/rules.md stays in lockstep with the rules tracelint actually ships.

Every rule has a stable anchor that the SARIF ``helpUri`` and the text report's reference line link
to. This test fails if a rule is added, removed, or renamed without a matching entry in the
reference page — so the help links can never point at a rule the page doesn't explain (or leave a
rule undocumented).
"""

from __future__ import annotations

import re
from pathlib import Path

from tracelint.rules import rule_ids
from tracelint.sarif import HELP_URI, _help_uri

RULES_DOC = Path(__file__).resolve().parent.parent / "docs" / "rules.md"


def _anchors(text: str) -> set[str]:
    return set(re.findall(r'<a id="([^"]+)"></a>', text))


def test_every_rule_has_an_anchor_and_there_are_no_orphans():
    documented = _anchors(RULES_DOC.read_text(encoding="utf-8"))
    expected = {rid.lower() for rid in rule_ids()}
    assert documented == expected


def test_each_rule_help_uri_targets_an_existing_anchor():
    anchors = _anchors(RULES_DOC.read_text(encoding="utf-8"))
    for rid in rule_ids():
        uri = _help_uri(rid)
        assert uri.startswith(HELP_URI + "#")
        assert uri.split("#", 1)[1] in anchors


def test_each_rule_section_declares_a_tier():
    text = RULES_DOC.read_text(encoding="utf-8")
    assert text.count("**Tier:**") == len(rule_ids())
