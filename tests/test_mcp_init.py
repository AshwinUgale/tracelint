"""`tracelint init --from-mcp` — a starter contract from a saved MCP tools/list (audit 3.3).

`inputSchema` becomes the argument schema; the `readOnlyHint` / `idempotentHint` annotations become
`side_effecting` / `idempotent`, but only when the server states them explicitly — they are advisory
(the MCP spec says clients must not rely on them) and `side_effecting` drives the hard-defect rules,
so a missing hint is a `_todo` to classify, never a guessed default that could manufacture a red.
"""

from __future__ import annotations

import json

import pytest

from tracelint.cli import main
from tracelint.contract import discover_mcp_contract
from tracelint.tools import ToolRegistry

_OBJ = {"type": "object"}


def _tool(name, schema=_OBJ, **annotations):
    tool = {"name": name}
    if schema is not None:
        tool["inputSchema"] = schema
    if annotations:
        tool["annotations"] = annotations
    return tool


def _draft(*tools):
    return discover_mcp_contract({"tools": list(tools)})


def test_explicit_write_hint_becomes_side_effecting():
    d = _draft(_tool("send", readOnlyHint=False))
    assert d.tools["send"]["metadata"] == {"side_effecting": True}


def test_read_only_hint_is_not_side_effecting_and_needs_no_behaviour_todo():
    d = _draft(_tool("peek", readOnlyHint=True))
    assert d.tools["peek"]["metadata"] == {}  # known read-only: side_effecting stays the safe false
    assert "_todo" not in d.tools["peek"]  # has a schema and a known effect: nothing to classify


def test_idempotent_hint_is_recorded():
    d = _draft(_tool("put", readOnlyHint=False, idempotentHint=True))
    assert d.tools["put"]["metadata"] == {"side_effecting": True, "idempotent": True}


def test_missing_hint_is_flagged_not_guessed():
    d = _draft(_tool("mystery"))
    assert "side_effecting" not in d.tools["mystery"]["metadata"]  # never guessed from a default
    assert any("side_effecting" in t for t in d.tools["mystery"]["_todo"])


def test_inputschema_becomes_the_schema_and_a_missing_one_is_a_todo():
    d = _draft(
        _tool("a", schema={"type": "object", "properties": {"x": {"type": "string"}}}),
        _tool("b", schema=None),
    )
    assert d.tools["a"]["schema"]["properties"] == {"x": {"type": "string"}}
    assert d.tools["b"]["schema"] is None
    assert any("JSON Schema" in t for t in d.tools["b"]["_todo"])


def test_envelope_variants_parse():
    tool = _tool("t")
    assert "t" in discover_mcp_contract({"result": {"tools": [tool]}}).tools  # JSON-RPC envelope
    assert "t" in discover_mcp_contract([tool]).tools  # a bare list


def test_not_an_mcp_payload_is_rejected():
    with pytest.raises(ValueError, match="tools/list"):
        discover_mcp_contract({"nope": 1})


def test_generated_contract_loads_as_a_registry():
    d = _draft(_tool("send", readOnlyHint=False), _tool("peek", readOnlyHint=True))
    reg = ToolRegistry.from_dict(d.to_dict())  # the _comment and _todo keys are tolerated
    assert reg.metadata_for("send").side_effecting is True
    assert reg.metadata_for("peek").side_effecting is False


def test_cli_init_from_mcp_writes_a_loadable_contract(tmp_path):
    mcp = tmp_path / "tools_list.json"
    mcp.write_text(json.dumps({"tools": [_tool("charge", readOnlyHint=False)]}), encoding="utf-8")
    out = tmp_path / "tools.json"
    assert main(["init", "--from-mcp", str(mcp), "-o", str(out)]) == 0
    assert ToolRegistry.load(str(out)).metadata_for("charge").side_effecting is True


def test_cli_init_rejects_both_trace_and_mcp(tmp_path):
    mcp = tmp_path / "m.json"
    mcp.write_text(json.dumps({"tools": []}), encoding="utf-8")
    tr = tmp_path / "t.json"
    tr.write_text(json.dumps({"run_id": "r", "steps": []}), encoding="utf-8")
    assert main(["init", str(tr), "--from-mcp", str(mcp)]) == 3  # exactly one source


def test_cli_init_needs_a_source(capsys):
    assert main(["init"]) == 3
    assert "from-mcp" in capsys.readouterr().err


def test_cli_init_rejects_a_non_mcp_file(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"nope": 1}), encoding="utf-8")
    assert main(["init", "--from-mcp", str(bad)]) == 3
