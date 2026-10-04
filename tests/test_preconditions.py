"""R9, declared preconditions: an action ran although a call it requires had failed or never ran.

R2b proves an agent acted on a failed result only when a value flows from the failure into the
action. The audit reproduced the gap with an ``UNSTABLE`` pipeline followed by a deploy: copying the
failed build's id was a hard defect, but deploying ``"latest"`` or a version number exited 0, though
the business defect is the same. A refund after a failed lookup of an id the user gave has the same
shape, and since R2b follows dataflow it is only a candidate.

Now a tool can declare what must succeed before it runs, in the same ``tools.json`` as
``side_effecting`` and ``failure_when``:
``"requires": [{"tool": "get_order", "same": ["order_id"]}]``. The **latest** returned call of the
required tool decides, so a retry that passes satisfies it and a later failure un-satisfies it,
and ``same`` scopes it to one entity. A required call still in flight doesn't count. An outcome
the trace can't show is disclosed as not checked, never passed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tracelint import ToolRegistry, build_trace, default_rules, lint_trace, load_source
from tracelint.cli import main
from tracelint.contract import discover_contract
from tracelint.findings import ConfidenceTier, LintReport
from tracelint.identity import finding_key
from tracelint.rules import ErrorHandlingRule, PreconditionRule
from tracelint.trace import Message, ResultStatus, Role, ToolCall, ToolResult

OK, ERROR, UNKNOWN = ResultStatus.OK, ResultStatus.ERROR, ResultStatus.UNKNOWN
PIPELINE = {
    "run_release_pipeline": {
        "metadata": {"failure_when": {"pointer": "/result", "in": ["FAILURE", "UNSTABLE"]}}
    }
}


def _deploy_contract(**requirement: Any) -> dict[str, Any]:
    deploy = {"side_effecting": True, "requires": [{"tool": "run_release_pipeline", **requirement}]}
    return {**PIPELINE, "deploy": {"metadata": deploy}}


def _r9(steps: list[Any], tools: dict[str, Any]) -> LintReport:
    registry = ToolRegistry.from_dict({"tools": tools})
    return lint_trace(build_trace("run", steps), [PreconditionRule()], registry)


def _pipeline(cid: str, version: str, result: str) -> list[Any]:
    return [
        ToolCall(cid, "run_release_pipeline", {"version": version}),
        ToolResult(cid, {"build_id": f"b-{version}", "result": result}, status=UNKNOWN),
    ]


def _deploy(args: dict[str, Any]) -> list[Any]:
    return [ToolCall("d", "deploy", args), ToolResult("d", {"status": "live"}, status=OK)]


USER = Message(Role.USER, "Ship release 4.12.0 to production.")


def _signals(report: LintReport) -> list[str]:
    return [f.evidence["signal"] for f in report.active_findings]


# --- The audit's case: the same defect, with or without a value flowing --------------------------


@pytest.mark.parametrize(
    "deploy_args",
    [{"build_id": "b-4.12.0"}, {"build_id": "latest"}, {"version": "4.12.0"}],
)
def test_deploying_after_an_unstable_pipeline_is_a_hard_defect(deploy_args):
    steps = [USER, *_pipeline("p", "4.12.0", "UNSTABLE"), *_deploy(deploy_args)]
    report = _r9(steps, _deploy_contract())

    (finding,) = report.active_findings
    assert finding.tier is ConfidenceTier.HARD_DEFECT
    assert finding.evidence["signal"] == "prerequisite_failed"
    assert "/result='UNSTABLE'" in finding.summary
    assert report.exit_code == 2


def test_r2b_alone_misses_the_deploy_of_latest():
    steps = [USER, *_pipeline("p", "4.12.0", "UNSTABLE"), *_deploy({"build_id": "latest"})]
    registry = ToolRegistry.from_dict({"tools": _deploy_contract()})
    report = lint_trace(build_trace("run", steps), [ErrorHandlingRule()], registry)
    assert report.exit_code == 0  # why R9 exists


# --- The latest call decides ---------------------------------------------------------------------


def test_a_retry_that_passes_satisfies_it():
    steps = [
        USER,
        *_pipeline("p1", "4.12.0", "UNSTABLE"),
        *_pipeline("p2", "4.12.0", "SUCCESS"),
        *_deploy({"version": "4.12.0"}),
    ]
    assert _r9(steps, _deploy_contract()).active_findings == []


def test_a_later_failure_un_satisfies_it():
    steps = [
        USER,
        *_pipeline("p1", "4.12.0", "SUCCESS"),
        *_pipeline("p2", "4.12.0", "UNSTABLE"),
        *_deploy({"version": "4.12.0"}),
    ]
    assert _signals(_r9(steps, _deploy_contract())) == ["prerequisite_failed"]


def test_same_scopes_it_to_one_entity():
    history = [*_pipeline("p1", "4.11.0", "SUCCESS"), *_pipeline("p2", "4.12.0", "UNSTABLE")]
    contract = _deploy_contract(same=["version"])
    assert _signals(_r9([USER, *history, *_deploy({"version": "4.12.0"})], contract)) == [
        "prerequisite_failed"
    ]
    assert _r9([USER, *history, *_deploy({"version": "4.11.0"})], contract).active_findings == []


def test_refunding_an_order_whose_own_lookup_never_ran():
    tools = {
        "get_order": {},
        "refund_order": {
            "metadata": {
                "side_effecting": True,
                "requires": [{"tool": "get_order", "same": ["order_id"]}],
            }
        },
    }
    steps = [
        Message(Role.USER, "Refund orders A100 and B200."),
        ToolCall("g", "get_order", {"order_id": "A100"}),
        ToolResult("g", {"order_id": "A100", "status": "shipped"}, status=OK),
        ToolCall("r", "refund_order", {"order_id": "B200"}),
    ]
    (finding,) = _r9(steps, tools).active_findings
    assert finding.evidence["signal"] == "prerequisite_missing"
    assert finding.evidence["same"] == {"order_id": "B200"}
    assert "for order_id='B200'" in finding.summary


def test_a_refund_after_a_failed_lookup_of_the_users_id():
    # The case R2b leaves as a candidate since it follows dataflow (the id came from the user).
    tools = {"refund_order": {"metadata": {"requires": [{"tool": "get_order"}]}}}
    steps = [
        Message(Role.USER, "Refund order A100."),
        ToolCall("g", "get_order", {"order_id": "A100"}),
        ToolResult("g", {"order_id": "A100", "status": "error"}, status=ERROR),
        ToolCall("r", "refund_order", {"order_id": "A100"}),
    ]
    assert _signals(_r9(steps, tools)) == ["prerequisite_failed"]


def test_an_action_with_no_required_call_before_it():
    report = _r9([USER, *_deploy({"version": "4.12.0"})], _deploy_contract())
    assert _signals(report) == ["prerequisite_missing"]
    assert "without a successful 'run_release_pipeline' first" in report.active_findings[0].summary


def test_a_required_call_still_in_flight_does_not_count():
    # Fired in parallel: the deploy went out before the pipeline's result came back.
    steps = [
        USER,
        ToolCall("p", "run_release_pipeline", {"version": "4.12.0"}),
        ToolCall("d", "deploy", {"version": "4.12.0"}),
        ToolResult("p", {"result": "SUCCESS"}, status=UNKNOWN),
        ToolResult("d", {"status": "live"}, status=OK),
    ]
    assert _signals(_r9(steps, _deploy_contract())) == ["prerequisite_missing"]


def test_succeeded_false_only_needs_the_call_to_have_returned():
    contract = _deploy_contract(succeeded=False)
    steps = [USER, *_pipeline("p", "4.12.0", "UNSTABLE"), *_deploy({"version": "4.12.0"})]
    assert _r9(steps, contract).active_findings == []
    report = _r9([USER, *_deploy({"version": "4.12.0"})], contract)
    assert "without a 'run_release_pipeline' call first" in report.active_findings[0].summary


# --- What can't be verified is disclosed, never passed -------------------------------------------


def test_an_outcome_the_trace_cannot_show_is_not_checked():
    # No error, unknown status, and the required tool declares no failure_when to read.
    tools = {"deploy": {"metadata": {"requires": [{"tool": "run_release_pipeline"}]}}}
    steps = [USER, *_pipeline("p", "4.12.0", "SUCCESS"), *_deploy({})]
    report = _r9(steps, tools)
    assert report.active_findings == [] and report.exit_code == 0
    (suppression,) = report.suppressions
    assert "declares no failure_when" in suppression.suppressed_reason
    (coverage,) = report.coverage
    assert (coverage.evaluatable, coverage.total) == (0, 1)


def test_a_failure_when_field_that_is_absent_is_not_checked():
    steps = [
        USER,
        ToolCall("p", "run_release_pipeline", {"version": "4.12.0"}),
        ToolResult("p", {"build_id": "b-1"}, status=UNKNOWN),  # no /result at all
        *_deploy({}),
    ]
    (suppression,) = _r9(steps, _deploy_contract()).suppressions
    assert "(/result) is absent" in suppression.suppressed_reason


def test_unknown_arguments_are_disclosed():
    contract = _deploy_contract(same=["version"])
    redacted_action = [ToolCall("d", "deploy", {}, args_unavailable="redacted")]
    report = _r9([USER, *_pipeline("p", "4.12.0", "SUCCESS"), *redacted_action], contract)
    assert report.active_findings == []
    assert "not checked for declared preconditions" in report.suppressions[0].suppressed_reason

    redacted_requirement = [
        ToolCall("p", "run_release_pipeline", {}, args_unavailable="redacted"),
        ToolResult("p", {"result": "UNSTABLE"}, status=UNKNOWN),
    ]
    report = _r9([USER, *redacted_requirement, *_deploy({"version": "4.12.0"})], contract)
    assert report.active_findings == []  # that call may be the 4.12.0 run: can't say it's missing
    assert "can't be identified" in report.suppressions[0].suppressed_reason


def test_nothing_declared_adds_nothing_to_the_report():
    steps = [USER, *_pipeline("p", "4.12.0", "UNSTABLE"), *_deploy({})]
    report = _r9(steps, PIPELINE)
    assert report.findings == [] and report.coverage == []


# --- The contract --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("requires", "message"),
    [
        ({"tool": "x"}, "requires must be a list"),
        (["x"], "each requires entry is an object"),
        ([{"tools": "x"}], "unknown key 'tools' in requires"),
        ([{"succeeded": True}], "needs the required tool's name"),
        ([{"tool": "x", "succeeded": "yes"}], "succeeded must be true or false"),
        ([{"tool": "x", "same": "order_id"}], "same must be a list of argument names"),
    ],
)
def test_a_malformed_requirement_is_an_error(requires, message):
    with pytest.raises(ValueError, match=message):
        ToolRegistry.from_dict({"tools": {"deploy": {"metadata": {"requires": requires}}}})


def test_a_malformed_requirement_fails_the_run(tmp_path, capsys):
    tools = tmp_path / "tools.json"
    tools.write_text(json.dumps({"tools": {"deploy": {"metadata": {"requires": "x"}}}}), "utf-8")
    trace = tmp_path / "t.json"
    trace.write_text(build_trace("run", [USER, *_deploy({})]).to_json(), encoding="utf-8")
    assert main(["check", str(trace), "--tools", str(tools)]) == 3
    assert "deploy: requires must be a list" in capsys.readouterr().err


def test_the_contract_view_lists_requirements():
    registry = ToolRegistry.from_dict({"tools": _deploy_contract(same=["version"])})
    contract = registry.contract_for("deploy")
    assert "requires:   a successful run_release_pipeline (same version)" in contract.describe()
    assert contract.to_dict()["requires"] == [{"tool": "run_release_pipeline", "same": ["version"]}]


def test_a_finding_key_names_both_tools():
    steps = [USER, *_pipeline("p", "4.12.0", "UNSTABLE"), *_deploy({"build_id": "latest"})]
    key = finding_key(_r9(steps, _deploy_contract()).active_findings[0])
    assert key.describe() == (
        "R9 unmet_precondition run_release_pipeline -> deploy (prerequisite_failed)"
    )


# --- Real trace and onboarding -------------------------------------------------------------------


def test_a_real_langgraph_release_agent():
    # The real Phoenix export behind the Arize post: the agent deployed an UNSTABLE build.
    path = (
        Path(__file__).resolve().parent.parent
        / "examples"
        / "traces"
        / "langgraph_phoenix_trace.json"
    )
    (trace,) = load_source(path, "openinference")
    registry = ToolRegistry.from_dict({"tools": _deploy_contract()})
    report = lint_trace(trace, default_rules(), registry)
    rules = sorted((f.rule, f.tier.value) for f in report.active_findings)
    assert ("R9", "hard_defect") in rules and ("R2b", "hard_defect") in rules


def test_init_proposes_a_requirement_for_a_call_that_follows_another():
    steps = [USER, *_pipeline("p", "4.12.0", "SUCCESS"), *_deploy({"version": "4.12.0"})]
    draft = discover_contract([build_trace("run", steps)])
    assert draft.after_others == ["deploy"]
    hint = draft.tools["deploy"]["_todo"][-1]
    assert "metadata.requires" in hint and '[{"tool": "run_release_pipeline"}]' in hint
    assert not any("requires" in t for t in draft.tools["run_release_pipeline"]["_todo"])
