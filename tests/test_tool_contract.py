"""Tool Contract — a coherent read-only view over the declared tool metadata (Item 4).

Presentation only: the four existing keys (`schema`, `side_effecting`, `failure_when`, per-field
`x-value-origin`) are grouped into one view. No new semantics — linting behaviour is unchanged.
"""

from __future__ import annotations

from tracelint import ToolContract, ToolRegistry
from tracelint.predicates import FailurePredicate


def _reg() -> ToolRegistry:
    return ToolRegistry.from_dict(
        {
            "tools": {
                "charge_card": {
                    "schema": {
                        "type": "object",
                        "properties": {
                            "account_id": {"type": "string", "x-value-origin": "provided"},
                            "request_id": {"type": "string", "x-value-origin": "generated"},
                        },
                    },
                    "metadata": {
                        "side_effecting": True,
                        "failure_when": {"pointer": "/status", "in": ["declined", "failed"]},
                    },
                },
                "get_order": {"metadata": {}},
            }
        }
    )


def test_contract_for_groups_the_four_sections():
    c = _reg().contract_for("charge_card")
    assert isinstance(c, ToolContract)
    assert c.name == "charge_card"
    assert c.side_effecting is True
    assert c.failure_when is not None
    assert c.failure_when.summary() == "/status in ['declined', 'failed']"
    assert c.value_origins == {"account_id": "provided", "request_id": "generated"}
    assert c.schema is not None


def test_contract_for_unknown_tool_is_none():
    assert _reg().contract_for("nope") is None


def test_contracts_lists_all_in_declaration_order():
    assert [c.name for c in _reg().contracts()] == ["charge_card", "get_order"]


def test_describe_presents_all_sections():
    text = _reg().contract_for("charge_card").describe()
    assert "args:" in text and "schema declared (2 properties)" in text
    assert "effects:" in text and "side-effecting" in text
    assert "failure:" in text and "/status in" in text
    assert "provenance:" in text and "account_id=provided" in text


def test_describe_minimal_contract_shows_none_declared():
    text = _reg().contract_for("get_order").describe()
    assert "no schema declared" in text
    assert "no declared side effect" in text
    assert "failure:    none declared" in text
    assert "provenance: none declared" in text


def test_to_dict_shape():
    d = _reg().contract_for("charge_card").to_dict()
    assert d["schema"] == {"declared": True, "properties": ["account_id", "request_id"]}
    assert d["effects"]["side_effecting"] is True and d["effects"]["idempotent"] is False
    assert d["failure_when"] == "/status in ['declined', 'failed']"
    assert d["provenance"] == {"account_id": "provided", "request_id": "generated"}


def test_predicate_summary_variants():
    def s(**kw: object) -> str:
        pred = FailurePredicate.from_dict({"pointer": "/x", **kw})
        assert pred is not None
        return pred.summary()

    assert s(**{"in": ["a", "b"]}) == "/x in ['a', 'b']"
    assert s(equals=False) == "/x == False"
    assert s(contains="Error:") == "/x contains 'Error:'"
    assert s(matches="^E") == "/x matches /^E/"
    assert s(exists=True) == "/x present"
    whole = FailurePredicate.from_dict({"pointer": "", "contains": "z"})
    assert whole is not None and whole.summary() == "(result) contains 'z'"
