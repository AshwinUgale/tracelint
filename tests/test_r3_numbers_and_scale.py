"""R3: digits come from one number, numbers compare by value, and long traces stay fast.

Up to 0.9, R3's provenance check had two problems:

- **Digits were matched across numbers.** It joined every digit in a text into one string, so a
  fabricated ``ORD-58213`` "derived" from a result holding a total of 58 and a quantity of 213,
  even on a field annotated ``provided``. Two observed numbers could also be run together with no
  separator (58 and 213 as 58213). Meanwhile the same number written differently was missed:
  ``1200.0`` against ``$1,200``, or a float ``4906.0`` against ``4906``, was reported as
  underivable, a hard defect on a ``provided`` field.
- **Its cost grew with the square of the trace.** It rebuilt the provenance graph for every call
  and compared every pair of observed values for each argument: one 1,000-row tool result and two
  free-text arguments took about a minute, and so did 1,000 small calls.

Now a value's digits must come from a single number in the text (a phone number written with
separators still counts), numbers are compared by value, and one graph is grown through the trace
with its values indexed.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from tracelint import ConfidenceTier, ToolRegistry, build_trace, lint_trace
from tracelint.provenance import ProvenanceGraph, ProvenanceNode, SourceType, build_provenance
from tracelint.rules import HallucinatedArgRule
from tracelint.trace import Message, ResultStatus, Role, ToolCall, ToolResult


def _derivable(observed: list[Any], value: Any) -> bool:
    steps = build_trace("r", [*observed, ToolCall("x", "act", {"v": value})]).steps
    return build_provenance(steps, steps[-1].index).derive(value).derivable


def _result(content: Any) -> ToolResult:
    return ToolResult("c0", content, status=ResultStatus.OK)


def _provided(field: str) -> ToolRegistry:
    schema = {"type": "object", "properties": {field: {"x-value-origin": "provided"}}}
    return ToolRegistry.from_dict({"tools": {"act": {"schema": schema}}})


# --- Digits come from one number ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("observed", "value"),
    [
        ([_result({"total": 58, "qty": 213})], "ORD-58213"),  # the audit's case
        ([_result({"total": 58, "qty": 213})], "58213"),  # two numbers run together
        ([_result({"ids": [1001, 1002]})], "10011002"),
        ([Message(Role.USER, "order 12 items, then 300 more")], "12300"),
    ],
)
def test_digits_are_not_assembled_from_several_numbers(observed, value):
    assert not _derivable(observed, value)


@pytest.mark.parametrize(
    ("text", "value"),
    [
        ("call me at (555) 123-4567", "5551234567"),
        ("call +1 (555) 123-4567", "+15551234567"),
        ("card 4242 4242 4242 4242", "4242"),
        ("ship on 2024-06-01", "20240601"),
        ("the total was 1234.56 dollars", "1,234.56"),
    ],
)
def test_digits_inside_one_number_still_derive(text, value):
    assert _derivable([Message(Role.USER, text)], value)


def test_values_joined_with_a_separator_or_after_letters_still_derive():
    observed = [_result({"first": "alpha", "second": "bravo", "prefix": "AB", "n": 123})]
    assert _derivable(observed, "alpha-bravo")
    assert _derivable(observed, "AB123")  # a letter-to-digit join is visible, unlike 58 + 213


def test_an_assembled_order_number_on_a_provided_field_is_a_hard_defect():
    # 0.9 derived it from the total and quantity and reported nothing.
    steps = [
        Message(Role.USER, "What did I order?"),
        ToolCall("c0", "get_cart", {}),
        _result({"total": 58, "qty": 213}),
        ToolCall("c1", "act", {"order_id": "ORD-58213"}),
    ]
    report = lint_trace(build_trace("r", steps), [HallucinatedArgRule()], _provided("order_id"))

    (finding,) = report.active_findings
    assert finding.tier is ConfidenceTier.HARD_DEFECT
    assert finding.evidence["value"] == "ORD-58213"


# --- Numbers compare by value ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("observed", "value"),
    [
        ([Message(Role.USER, "refund $1,200 please")], 1200.0),
        ([_result({"amount": 4906})], 4906.0),
        ([_result({"amount": "€35,344"})], 35344),
        ([Message(Role.USER, "order 12 items")], 12),
        ([Message(Role.USER, "The total is 1200.")], "1,200.00"),
    ],
)
def test_the_same_number_written_differently_derives(observed, value):
    assert _derivable(observed, value)


@pytest.mark.parametrize(
    ("observed", "value"),
    [
        ([Message(Role.USER, "order 120 items")], 12),
        ([Message(Role.USER, "upgrade to v1.2.3")], "1.20"),  # part of a version, not a number
        ([Message(Role.USER, "invoice INV2024")], 2024.0),  # part of a word
    ],
)
def test_a_number_inside_something_else_is_not_that_number(observed, value):
    assert not _derivable(observed, value)


def test_a_reformatted_amount_on_a_provided_field_is_not_a_defect():
    # 0.9 reported 1200.0 as underivable from "$1,200": a hard defect on a correct call.
    steps = [
        Message(Role.USER, "Refund $1,200 to my card."),
        ToolCall("c1", "act", {"amount": 1200.0}),
    ]
    report = lint_trace(build_trace("r", steps), [HallucinatedArgRule()], _provided("amount"))
    assert report.active_findings == []
    assert report.exit_code == 0


# --- One graph, grown through the trace --------------------------------------------------------


def test_each_call_sees_only_what_came_before_it():
    steps = [
        Message(Role.USER, "look up my order"),
        ToolCall("c1", "lookup", {"order_id": "ORD-7731"}),  # not seen yet: flagged
        ToolResult("c1", {"order_id": "ORD-7731"}, status=ResultStatus.OK),
        ToolCall("c2", "refund", {"order_id": "ORD-7731"}),  # seen in the result: fine
    ]
    findings = lint_trace(build_trace("r", steps), [HallucinatedArgRule()], ToolRegistry())
    assert [f.evidence["step_indices"] for f in findings.active_findings] == [[1]]


def test_observe_matches_build_provenance():
    trace = build_trace(
        "r",
        [
            Message(Role.SYSTEM, "store 44"),
            Message(Role.USER, "price check for SKU-1001"),
            ToolCall("c1", "price", {"sku": "SKU-1001"}),
            ToolResult("c1", {"sku": "SKU-1001", "price": "$1,999.00"}, status=ResultStatus.OK),
        ],
    )
    grown = ProvenanceGraph()
    for step in trace.steps:
        grown.observe(step)
    built = build_provenance(trace.steps, len(trace.steps))
    for value in ["SKU-1001", 1999, "1999.0", "store 44", "price", "SKU-2002", 44]:
        assert grown.derive(value) == built.derive(value), value


def test_nodes_added_directly_are_indexed():
    graph = ProvenanceGraph()
    graph.nodes.append(ProvenanceNode("CUST-4410", SourceType.TOOL, 0))
    assert graph.derive("CUST-4410").derivable
    graph.nodes.append(ProvenanceNode("CUST-5520", SourceType.TOOL, 1))
    assert graph.derive("cust-5520").derivable
    graph.nodes = [ProvenanceNode("NEW-1", SourceType.TOOL, 0)]  # replaced, not appended
    assert not graph.derive("CUST-4410").derivable


# --- Cost --------------------------------------------------------------------------------------

# A minute or more each on 0.9; well under a second now. The bound leaves room for slow CI runners.
BUDGET_SECONDS = 10.0


def _timed_r3(trace) -> float:
    start = time.perf_counter()
    lint_trace(trace, [HallucinatedArgRule()], ToolRegistry())
    return time.perf_counter() - start


def test_a_large_tool_result_does_not_square_the_cost():
    rows = [
        {"id": f"P{i:05d}", "price": 100 + i, "stock": i % 50, "sku": f"S-{i}"} for i in range(1000)
    ]
    steps = [
        Message(Role.USER, "find me a laptop"),
        ToolCall("s", "search", {"q": "laptop"}),
        ToolResult("s", {"results": rows}, status=ResultStatus.OK),
    ]
    for k in range(2):
        steps += [
            ToolCall(f"c{k}", "add_to_cart", {"note": f"gift wrap please #{k}xx"}),
            ToolResult(f"c{k}", {"ok": True}, status=ResultStatus.OK),
        ]
    assert _timed_r3(build_trace("big-result", steps)) < BUDGET_SECONDS


def test_a_long_trace_does_not_square_the_cost():
    steps: list[Any] = [Message(Role.USER, "do the work")]
    for i in range(1000):
        steps += [
            ToolCall(f"c{i}", "act", {"item": f"ITEM-{i:05d}"}),
            ToolResult(
                f"c{i}", {"next": f"ITEM-{i + 1:05d}", "blob": "x" * 200}, status=ResultStatus.OK
            ),
        ]
    assert _timed_r3(build_trace("long", steps)) < BUDGET_SECONDS
