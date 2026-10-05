"""SARIF 2.1.0 output for GitHub code scanning (issue #13).

tracelint's tiers map into SARIF's ``level`` vocabulary (hard_defect->error, hard_event->warning,
candidate->note), suppressions are omitted (they are not defects), and every result carries a
stable ``partialFingerprints`` identity plus the trace's ``step_indices``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tracelint.cli import main
from tracelint.findings import ConfidenceTier, Finding, LintReport
from tracelint.sarif import SARIF_VERSION, build_line_map, to_sarif


def _f(rule, ftype, tier, steps=(0,), summary="something happened"):
    return Finding(
        rule=rule,
        finding_type=ftype,
        tier=tier,
        summary=summary,
        evidence={"step_indices": list(steps)},
    )


def _report():
    return LintReport(
        run_id="run-1",
        findings=[
            _f("R1", "schema_violation", ConfidenceTier.HARD_DEFECT),
            _f("R2a", "tool_error_event", ConfidenceTier.HARD_EVENT),
            _f("R3", "hallucinated_arg", ConfidenceTier.CANDIDATE),
            Finding.suppressed("R6", "malformed_arguments", "no arguments recorded"),
        ],
    )


def _levels_by_rule(results):
    return {r["ruleId"]: r["level"] for r in results}


def test_envelope_shape():
    sarif = to_sarif([_report()], tool_version="9.9.9", uris=["trace.json"])
    assert sarif["version"] == SARIF_VERSION == "2.1.0"
    assert sarif["$schema"].endswith("sarif-2.1.0.json")
    driver = sarif["runs"][0]["tool"]["driver"]
    assert driver["name"] == "tracelint"
    assert driver["version"] == "9.9.9"
    assert driver["informationUri"].endswith("AshwinUgale/tracelint")


def test_tier_to_level_mapping():
    results = to_sarif([_report()], tool_version="0", uris=["t.json"])["runs"][0]["results"]
    levels = _levels_by_rule(results)
    assert levels == {"R1": "error", "R2a": "warning", "R3": "note"}


def test_suppressions_are_not_results():
    results = to_sarif([_report()], tool_version="0", uris=["t.json"])["runs"][0]["results"]
    # 4 findings in, but the R6 suppression is excluded -> 3 results.
    assert len(results) == 3
    assert "R6" not in _levels_by_rule(results)


def test_locations_use_the_source_uri():
    results = to_sarif([_report()], tool_version="0", uris=["path/to/trace.json"])["runs"][0][
        "results"
    ]
    for r in results:
        loc = r["locations"][0]["physicalLocation"]
        assert loc["artifactLocation"]["uri"] == "path/to/trace.json"
        assert loc["region"]["startLine"] == 1


def test_uri_falls_back_to_run_id():
    results = to_sarif([_report()], tool_version="0")["runs"][0]["results"]
    assert all(r["locations"][0]["physicalLocation"]["artifactLocation"]["uri"] == "run-1"
               for r in results)


def test_rules_catalogue_and_indices():
    run = to_sarif([_report()], tool_version="0", uris=["t.json"])["runs"][0]
    rules = run["tool"]["driver"]["rules"]
    ids = [rule["id"] for rule in rules]
    assert ids == ["R1", "R2a", "R3"]  # first-seen order, suppression excluded
    # ruleIndex on each result points at the matching rule descriptor.
    for r in run["results"]:
        assert rules[r["ruleIndex"]]["id"] == r["ruleId"]
    # descriptors carry a default configuration level and a help link.
    r1 = next(rule for rule in rules if rule["id"] == "R1")
    assert r1["defaultConfiguration"]["level"] == "error"
    assert r1["name"] == "SchemaViolation"
    assert r1["helpUri"]


def test_step_indices_and_fingerprint_in_properties():
    r = to_sarif([_report()], tool_version="0", uris=["t.json"])["runs"][0]["results"][0]
    assert r["properties"]["step_indices"] == [0]
    assert r["properties"]["tier"] == "hard_defect"
    assert r["partialFingerprints"]["tracelintFinding/v1"]


def test_fingerprint_is_stable_across_runs():
    a = to_sarif([_report()], tool_version="0", uris=["t.json"])
    b = to_sarif([_report()], tool_version="0", uris=["t.json"])
    fa = [x["partialFingerprints"] for x in a["runs"][0]["results"]]
    fb = [x["partialFingerprints"] for x in b["runs"][0]["results"]]
    assert fa == fb


def test_invocation_execution_successful_tracks_hard_defect():
    with_defect = to_sarif([_report()], tool_version="0", uris=["t.json"])
    assert with_defect["runs"][0]["invocations"][0]["executionSuccessful"] is False

    clean = LintReport(
        run_id="ok",
        findings=[_f("R2a", "tool_error_event", ConfidenceTier.HARD_EVENT)],
    )
    ok = to_sarif([clean], tool_version="0", uris=["t.json"])
    assert ok["runs"][0]["invocations"][0]["executionSuccessful"] is True


def test_uris_length_mismatch_raises():
    with pytest.raises(ValueError, match="same length"):
        to_sarif([_report()], tool_version="0", uris=["a.json", "b.json"])


# --- CLI integration -------------------------------------------------------------------

def _planted_trace(tmp_path):
    from tracelint.agent import ReActAgent, ScriptedLLM, build_demo_toolset, final, tool

    toolset = build_demo_toolset()
    script = [tool("cancel_order", {"order_id": 4521, "reason": "fraud"}), final("done")]
    trace = ReActAgent(ScriptedLLM(script), toolset).run("cancel", run_id="planted")
    tp = tmp_path / "trace.json"
    tp.write_text(trace.to_json(), encoding="utf-8")
    specs = {n: {"schema": toolset.to_registry().get(n).schema} for n in toolset.names()}
    ttp = tmp_path / "tools.json"
    ttp.write_text(json.dumps({"tools": specs}), encoding="utf-8")
    return str(tp), str(ttp)


def test_cli_writes_valid_sarif_file(tmp_path):
    tp, ttp = _planted_trace(tmp_path)
    out = tmp_path / "results.sarif"
    code = main(["check", tp, "--tools", ttp, "--sarif", str(out), "--quiet"])
    assert code == 2  # a hard_defect was found

    sarif = json.loads(out.read_text(encoding="utf-8"))
    assert sarif["version"] == "2.1.0"
    results = sarif["runs"][0]["results"]
    assert results, "expected at least one result"
    assert any(r["level"] == "error" for r in results)
    # the finding is located in the trace file we passed on the command line.
    assert results[0]["locations"][0]["physicalLocation"]["artifactLocation"]["uri"] == tp
    assert sarif["runs"][0]["invocations"][0]["executionSuccessful"] is False
    # anchored to the finding's step line in the trace file, not the hardcoded line 1.
    region = results[0]["locations"][0]["physicalLocation"]["region"]
    assert region["startLine"] > 1
    assert region["startLine"] <= len(Path(tp).read_text(encoding="utf-8").splitlines())


def test_help_uri_is_per_rule_anchored():
    from tracelint.sarif import _RULE_META, HELP_URI, _help_uri

    # every rule links to its own anchor in the rules-reference page, lowercased id.
    for rid in _RULE_META:
        assert _help_uri(rid) == f"{HELP_URI}#{rid.lower()}"
    # distinct per rule, all under the one reference page.
    uris = {_help_uri(rid) for rid in _RULE_META}
    assert len(uris) == len(_RULE_META)
    assert all(u.startswith(HELP_URI + "#") for u in uris)
    # an unknown/external rule links to the page itself, not a dangling anchor.
    assert _help_uri("R999") == HELP_URI
    # and the emitted descriptors carry those links (R2a -> #r2a).
    rules = to_sarif([_report()], tool_version="0", uris=["t.json"])["runs"][0]["tool"]["driver"][
        "rules"
    ]
    assert next(r for r in rules if r["id"] == "R1")["helpUri"] == HELP_URI + "#r1"
    assert next(r for r in rules if r["id"] == "R2a")["helpUri"] == HELP_URI + "#r2a"


# --- startLine: locate a finding's step in the trace file ------------------------------

def _tc(call_id, *, span=None):
    from tracelint.trace import SourceRef, ToolCall

    return ToolCall(
        call_id=call_id, name="t", source=(SourceRef(span_id=span) if span else None)
    )


def _trace(*steps):
    from tracelint.trace import Trace

    return Trace(run_id="r", steps=list(steps))


def test_build_line_map_locates_by_span_then_call_id():
    tr = _trace(_tc("c1", span="SPAN_AAA"), _tc("c2"), _tc("cZ"))
    text = "\n".join(["line1", 'x "SPAN_AAA" y', "line3", '  "call_id": "c2"', "line5"])
    # step0 by span -> line 2; step1 by call_id -> line 4; step2's "cZ" absent -> omitted.
    assert build_line_map(tr, text, {0, 1, 2}) == {0: 2, 1: 4}


def test_build_line_map_prefers_span_and_honors_allow_call_id():
    tr = _trace(_tc("c1", span="SPAN"), _tc("c2"))
    text = '"SPAN"\n"c1"\n"c2"\n'  # lines 1,2,3
    # prefers the span line (1) over the call_id line (2) for step0.
    assert build_line_map(tr, text, {0}) == {0: 1}
    # with call_id disabled (a multi-trace file), step0 still resolves by span, step1 drops out.
    assert build_line_map(tr, text, {0, 1}, allow_call_id=False) == {0: 1}


def test_build_line_map_skips_empty_and_out_of_range():
    tr = _trace(_tc("c1"))
    assert build_line_map(tr, '"c1"', set()) == {}  # nothing requested -> no scan
    assert build_line_map(tr, '"c1"', {5}) == {}  # out of range -> skipped


def test_line_maps_set_the_result_start_line():
    out = to_sarif([_report()], tool_version="0", uris=["t.json"], line_maps=[{0: 7}])
    regions = [r["locations"][0]["physicalLocation"]["region"] for r in out["runs"][0]["results"]]
    assert regions and all(reg["startLine"] == 7 for reg in regions)


def test_unmapped_finding_defaults_to_line_one():
    out = to_sarif([_report()], tool_version="0", uris=["t.json"], line_maps=[{}])
    assert all(
        r["locations"][0]["physicalLocation"]["region"]["startLine"] == 1
        for r in out["runs"][0]["results"]
    )


def test_line_maps_length_mismatch_raises():
    with pytest.raises(ValueError, match="same length"):
        to_sarif([_report()], tool_version="0", line_maps=[{}, {}])
