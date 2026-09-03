"""SARIF 2.1.0 output for GitHub code scanning (issue #13).

tracelint's tiers map into SARIF's ``level`` vocabulary (hard_defect->error, hard_event->warning,
candidate->note), suppressions are omitted (they are not defects), and every result carries a
stable ``partialFingerprints`` identity plus the trace's ``step_indices``.
"""

from __future__ import annotations

import json

import pytest

from tracelint.cli import main
from tracelint.findings import ConfidenceTier, Finding, LintReport
from tracelint.sarif import SARIF_VERSION, to_sarif


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
