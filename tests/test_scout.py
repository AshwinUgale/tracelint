"""The Inspect Scout integration's pure core: a transcript's messages in, one result per rule out.

Runs without Inspect Scout (the transcript fixture is Inspect's JSON form). The scanners themselves,
on Scout's own objects and through ``scan()``, are tested in ``test_scout_scanners.py``.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

from tracelint.adapters import from_inspect_messages
from tracelint.contract import discover_mcp_contract
from tracelint.integrations.scout import (
    CONTRACT_RULES,
    KEYLESS_RULES,
    check_messages,
    lint_messages,
    resolve_rules,
)
from tracelint.record import check_record
from tracelint.tools import ToolRegistry

FIXTURES = Path(__file__).parent / "fixtures" / "scout"
TOOLS = FIXTURES / "tools.json"


@pytest.fixture
def transcript():
    return json.loads((FIXTURES / "transcript.json").read_text(encoding="utf-8"))


def _by_rule(results):
    return {r.rule: r for r in results}


def test_default_runs_the_keyless_rules_and_cites_messages(transcript):
    results = lint_messages(transcript["messages"], events=transcript["events"])
    assert [r.rule for r in results] == list(KEYLESS_RULES)
    by = _by_rule(results)

    errors = by["R2a"]
    assert errors.label == "tool_error_event"
    assert errors.value == 4
    # the timeout and the parse failure are on the messages; the email's failure is only on its
    # tool event; the empty web search result is a heuristic
    assert errors.metadata["tiers"] == {"hard_event": 3, "candidate": 1}
    assert [c.cite for c in errors.citations] == ["[M4]", "[M12]", "[M13]", "[M14]"]
    assert [c.message_id for c in errors.citations] == ["m-t1", "m-t5", "m-a6", "m-t6"]
    assert "[M14] (hard_event) 'send_email' returned an error" in errors.explanation

    loop = by["R4"]
    assert (loop.label, loop.value) == ("loop", 1)
    assert [c.cite for c in loop.citations] == ["[M5]", "[M7]", "[M9]"]
    assert loop.metadata["findings"][0]["messages"] == ["[M5]", "[M7]", "[M9]"]

    assert (by["R5"].label, by["R5"].value) == ("redundant_call", 1)

    malformed = by["R6"]
    assert (malformed.label, malformed.value) == ("malformed_arguments", 1)
    assert malformed.metadata["tiers"] == {"hard_defect": 1}
    assert [c.cite for c in malformed.citations] == ["[M11]"]


def test_a_contract_adds_the_contract_rules(transcript):
    results = lint_messages(transcript["messages"], events=transcript["events"], tools=TOOLS)
    assert [r.rule for r in results] == list(KEYLESS_RULES) + list(CONTRACT_RULES)
    by = _by_rule(results)
    # the server-side web search named the declared off-limits source
    assert by["R13"].value == 1
    assert by["R13"].metadata["tiers"] == {"hard_defect": 1}
    assert [c.cite for c in by["R13"].citations] == ["[M13]"]
    # the failed, side-effecting email was never retried
    assert by["R12"].value == 1
    # the web search is not in the contract: R1 says so rather than passing it
    assert by["R1"].metadata["not_checked"] == ["no schema for tool 'web_search'"]
    assert "Not checked: no schema for tool 'web_search'" in by["R1"].explanation
    assert by["R8"].value == 0 and by["R8"].explanation == "No duplicate side effect found."


def test_contract_as_a_dict_or_registry(transcript):
    data = json.loads(TOOLS.read_text(encoding="utf-8"))
    for tools in (data, ToolRegistry.from_dict(data)):
        by = _by_rule(lint_messages(transcript["messages"], tools=tools))
        assert by["R13"].value == 1


def test_a_contract_from_init_from_mcp_works(transcript):
    tools_list = {
        "tools": [
            {
                "name": "send_email",
                "inputSchema": {
                    "type": "object",
                    "properties": {"to": {"type": "string"}, "body": {"type": "string"}},
                    "required": ["to", "body"],
                },
                "annotations": {"readOnlyHint": False, "destructiveHint": False},
            }
        ]
    }
    contract = discover_mcp_contract(tools_list).to_dict()
    by = _by_rule(lint_messages(transcript["messages"], tools=contract))
    assert "R13" not in by  # the contract declares no off_limits
    assert by["R1"].value == 0  # send_email's arguments match its schema
    assert by["R7"].value == 6  # every call to the four tools the contract does not declare


def test_r13_is_left_out_without_off_limits_and_disclosed_when_asked_for(transcript):
    assert "R13" not in resolve_rules(None, ToolRegistry())
    assert "R13" in resolve_rules(None, ToolRegistry.load(TOOLS))
    (r13,) = lint_messages(transcript["messages"], rules=["R13"])
    assert r13.value is None
    assert r13.explanation == "Not checked: the contract declares no off_limits sources."


def test_a_rule_the_contract_cannot_feed_is_not_checked_rather_than_clean(transcript):
    (r1,) = lint_messages(transcript["messages"], rules=["R1"])
    assert r1.value is None
    assert r1.explanation.startswith("Not checked: no tool schema available")
    assert r1.metadata["not_checked"]


def test_a_rule_with_nothing_to_check_is_a_true_zero():
    messages = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]
    by = _by_rule(lint_messages(messages))
    assert by["R4"].value == 0
    assert by["R4"].explanation.startswith("Nothing to check:")
    assert by["R2a"].value == 0


def test_min_tier_counts_only_findings_at_or_above_it(transcript):
    kwargs = {"events": transcript["events"], "min_tier": "hard_event"}
    by = _by_rule(lint_messages(transcript["messages"], **kwargs))
    assert by["R2a"].value == 3  # the empty-result candidate is not counted
    assert by["R4"].value == 0 and by["R4"].explanation == "No loop found."
    by = _by_rule(lint_messages(transcript["messages"], min_tier="hard_defect"))
    assert (by["R2a"].value, by["R6"].value) == (0, 1)


def test_rules_as_a_comma_separated_string_and_unknown_rules():
    assert resolve_rules("R4, R5", None) == ["R4", "R5"]
    assert resolve_rules(["R6", "R6"], None) == ["R6"]
    with pytest.raises(ValueError, match="unknown rule 'R99'"):
        resolve_rules(["R99"], None)


def test_bad_tools_argument_is_rejected():
    with pytest.raises(TypeError, match="tools must be"):
        lint_messages([], tools=42)


def test_explanation_lists_a_bounded_number_of_findings():
    messages = []
    for i in range(30):
        messages.append(
            {"id": f"a{i}", "role": "assistant", "content": "", "tool_calls": [
                {"id": f"c{i}", "function": "f", "arguments": {"i": i}}
            ]}
        )
        messages.append(
            {"id": f"t{i}", "role": "tool", "tool_call_id": f"c{i}", "content": "",
             "error": {"type": "unknown", "message": "boom"}}
        )
    (r2a,) = lint_messages(messages, rules=["R2a"])
    assert r2a.value == 30
    assert len(r2a.metadata["findings"]) == 30
    assert r2a.explanation.splitlines()[-1] == "... and 5 more (see metadata)"


def test_the_registry_module_imports_without_scout():
    # The inspect_ai entry point is installed with tracelint whether or not the [scout] extra is,
    # so the module Inspect imports must never fail for want of Scout.
    importlib.import_module("tracelint.integrations.scout._registry")


# --- record quality -----------------------------------------------------------------------------


def _incomplete_record():
    """A transcript whose record has every structural gap ``record_quality`` reports: a call with
    no result (the run stops on it), a web search recorded with broken arguments, a placeholder
    turn, a reused call id, a redacted value, and two subagent launches of which Scout kept only
    the first as an agent span."""
    doc = json.loads((FIXTURES / "incomplete_record.json").read_text(encoding="utf-8"))
    return doc["messages"], doc["events"]


def test_record_quality_of_a_complete_record(transcript):
    record = check_messages(transcript["messages"], events=transcript["events"])
    assert record.value == []
    assert record.explanation == "No record issues found (7 tool calls, 7 with a result)."
    assert record.metadata["coverage"]["results"] == 100.0
    # the three failures: Inspect has no field that records a success
    assert record.metadata["outcome_recorded"] == 3


def test_record_quality_reports_each_gap_with_its_messages():
    messages, events = _incomplete_record()
    record = check_messages(messages, events=events)
    assert record.value == [
        "results_missing",
        "arguments_missing",
        "placeholder_turns",
        "ends_unanswered",
        "subagents_missing",
        "duplicate_call_ids",
        "redactions",
    ]
    assert record.explanation.splitlines() == [
        "1/6 calls have no result: [M10]",
        "1/6 calls have no arguments: [M9]",
        "1/1 assistant turns are placeholders (heuristic): [M2]",
        "ends on a call with no result: [M10]",
        "1 subagent run(s) not recorded (heuristic): [M4]",
        "1 duplicate call id(s): [M7]",
        "1 redacted value(s)",
    ]
    assert record.metadata["heuristic"] == ["placeholder_turns", "subagents_missing"]
    assert record.metadata["subagent_runs"] == 1  # the span Scout kept counts as recorded
    assert [c.message_id for c in record.citations] == ["a1", "a2", "a3", "a4", "a5"]


def test_record_quality_without_the_subagent_spans():
    messages, _ = _incomplete_record()
    record = check_messages(messages)
    assert "2 subagent run(s) not recorded (heuristic): [M4]" in record.explanation.splitlines()


def test_record_quality_of_an_empty_transcript():
    record = check_messages([])
    assert record.value == ["empty"]
    assert record.explanation == "empty: no steps"


def test_record_issue_codes_match_doctors_phrases():
    messages, events = _incomplete_record()
    check = check_record(from_inspect_messages(messages, events=events))
    assert [i.text for i in check.record_issues()] == check.issues()
