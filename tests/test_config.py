"""The CI contract: a project config file, ``--fail-on``, and ignores with reasons.

Up to 0.10, every setting had to be repeated on the command line of each CI step, there was no way
to fail CI on anything below a hard defect (exit 1 was reserved but unused), and no way to accept a
known finding except dropping its rule entirely. Now ``[tool.tracelint]`` in ``pyproject.toml`` (or
a ``tracelint.toml``) holds the defaults, ``fail_on`` opts into a lower gate, and an ignore accepts
one kind of finding with a reason, still shown in the report.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tracelint import build_trace
from tracelint.cli import main
from tracelint.config import ConfigError, find_config, load_config
from tracelint.findings import ConfidenceTier, Finding, LintReport
from tracelint.identity import finding_key
from tracelint.trace import Message, ResultStatus, Role, ToolCall, ToolResult

TOOLS = {"tools": {"refund_order": {"metadata": {"side_effecting": True}}}}


def _refund_after_failed_lookup():
    # R2a hard_event (get_order failed) + R2b hard_defect (refund used the cached payment method).
    return build_trace(
        "refund",
        [
            Message(Role.USER, "Refund order A100 to the card on file."),
            ToolCall("c1", "get_order", {"order_id": "A100"}),
            ToolResult(
                "c1",
                {"order_id": "A100", "status": "error", "payment_method": "pm_7731"},
                status=ResultStatus.ERROR,
            ),
            ToolCall("c2", "refund_order", {"order_id": "A100", "payment_method": "pm_7731"}),
            ToolResult("c2", {"refunded": True}, status=ResultStatus.OK),
        ],
    )


def _retried_error():
    # Only a hard_event: the lookup fails, is retried, and succeeds.
    return build_trace(
        "retried",
        [
            Message(Role.USER, "Look up order A100."),
            ToolCall("c1", "get_order", {"order_id": "A100"}),
            ToolResult("c1", "upstream timeout", status=ResultStatus.ERROR),
            ToolCall("c2", "get_order", {"order_id": "A100"}),
            ToolResult("c2", {"order_id": "A100", "status": "shipped"}, status=ResultStatus.OK),
        ],
    )


def _unexplained_argument():
    # Only a candidate: R3 cannot trace the confirmation id to anything the agent saw.
    return build_trace(
        "itinerary",
        [
            Message(Role.USER, "Send me my itinerary."),
            ToolCall("c1", "send_itinerary", {"confirmation_id": "CONF-4821"}),
            ToolResult("c1", {"sent": True}, status=ResultStatus.OK),
        ],
    )


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A repository with traces and a tools.json; the test writes the config."""
    (tmp_path / ".git").mkdir()
    traces = tmp_path / "traces"
    traces.mkdir()
    for name, trace in [
        ("refund.json", _refund_after_failed_lookup()),
        ("retried.json", _retried_error()),
        ("itinerary.json", _unexplained_argument()),
    ]:
        (traces / name).write_text(trace.to_json(), encoding="utf-8")
    (tmp_path / "tools.json").write_text(json.dumps(TOOLS), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _pyproject(root: Path, body: str) -> None:
    (root / "pyproject.toml").write_text(f'[project]\nname = "app"\n\n{body}', encoding="utf-8")


def _check(*args: str) -> int:
    return main(["check", *args])


# --- The default gate is unchanged ---------------------------------------------------------------


def test_without_a_config_only_a_hard_defect_fails(project):
    assert _check("traces/retried.json") == 0  # a hard_event alone
    assert _check("traces/itinerary.json") == 0  # a candidate alone
    assert _check("traces/refund.json", "--tools", "tools.json") == 2


def test_the_library_report_keeps_its_exit_code():
    report = LintReport("r", [Finding("R2a", "tool_error_event", ConfidenceTier.HARD_EVENT, "x")])
    assert report.exit_code == 0
    assert "fail_on" not in report.to_dict()


# --- fail_on -------------------------------------------------------------------------------------


def test_fail_on_hard_event_exits_1(project, capsys):
    _pyproject(project, '[tool.tracelint]\nfail_on = "hard_event"\n')
    assert _check("traces/retried.json") == 1
    assert "exit 1" in capsys.readouterr().out
    assert _check("traces/itinerary.json") == 0  # a candidate is below the gate


def test_fail_on_candidate_exits_1_and_shows_the_candidate(project, capsys):
    assert _check("traces/itinerary.json", "--fail-on", "candidate") == 1
    out = capsys.readouterr().out
    assert "R3 hallucinated_arg" in out  # never hide what fails the run
    assert "hidden" not in out


def test_a_hard_defect_still_exits_2_under_a_lower_gate(project):
    _pyproject(project, '[tool.tracelint]\nfail_on = "candidate"\ntools = "tools.json"\n')
    assert _check("traces/refund.json") == 2


def test_the_overall_exit_is_the_worst_trace(project):
    _pyproject(project, '[tool.tracelint]\nfail_on = "hard_event"\n')
    assert _check("traces/itinerary.json", "traces/retried.json") == 1
    assert _check("traces/itinerary.json", "traces/refund.json", "--tools", "tools.json") == 2


def test_json_output_carries_the_gate(project, tmp_path):
    out = tmp_path / "out.json"
    assert _check("traces/retried.json", "--fail-on", "hard_event", "--json", str(out)) == 1
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["overall_exit"] == 1
    assert data["reports"][0]["fail_on"] == "hard_event"


# --- Ignores -------------------------------------------------------------------------------------


IGNORE_DEPLOY = """
[tool.tracelint]
tools = "tools.json"

[[tool.tracelint.ignore]]
rule = "R2b"
tool = "refund_order"
reason = "refunds to the cached card are approved for this agent"
"""


def test_an_ignored_hard_defect_is_shown_but_does_not_fail(project, capsys):
    _pyproject(project, IGNORE_DEPLOY)
    assert _check("traces/refund.json") == 0
    out = capsys.readouterr().out
    assert "ignored (1) — not failing" in out
    assert "refunds to the cached card are approved for this agent" in out
    assert "R2a tool_error_event" in out  # the event is not ignored


def test_ignored_findings_are_marked_in_json(project, tmp_path):
    _pyproject(project, IGNORE_DEPLOY)
    out = tmp_path / "out.json"
    assert _check("traces/refund.json", "--json", str(out)) == 0
    report = json.loads(out.read_text(encoding="utf-8"))["reports"][0]
    assert all("ignored_reason" not in f for f in report["findings"])  # not folded into findings
    (ignored,) = report["ignored"]
    assert ignored["rule"] == "R2b" and ignored["tier"] == "hard_defect"


@pytest.mark.parametrize(
    ("narrowing", "matches"),
    [
        ('tool = "refund_order"', True),
        ('tool = "get_order"', True),  # R2b names both tools: the failed one and the consumer
        ('tool = "send_email"', False),
        ('path = "traces/refund*"', True),
        ('path = "traces/legacy/*"', False),
    ],
)
def test_an_ignore_can_be_narrowed(project, narrowing, matches):
    _pyproject(
        project,
        f'[tool.tracelint]\ntools = "tools.json"\n\n[[tool.tracelint.ignore]]\nrule = "R2b"\n'
        f'{narrowing}\nreason = "accepted"\n',
    )
    assert _check("traces/refund.json") == (0 if matches else 2)


def test_an_ignore_narrowed_to_a_field(project):
    _pyproject(
        project,
        '[[tool.tracelint.ignore]]\nrule = "R3"\nfield = "confirmation_id"\nreason = "generated"\n',
    )
    assert _check("traces/itinerary.json", "--fail-on", "candidate") == 0


def test_an_unused_ignore_is_reported(project, capsys):
    _pyproject(project, '[[tool.tracelint.ignore]]\nrule = "R8"\nreason = "old"\n')
    assert _check("traces/retried.json") == 0
    assert "ignore #1 (R8) matched no finding" in capsys.readouterr().err


# --- Discovery and precedence --------------------------------------------------------------------


def test_the_nearest_config_wins_and_tracelint_toml_comes_first(project):
    _pyproject(project, '[tool.tracelint]\nfail_on = "candidate"\n')
    (project / "tracelint.toml").write_text('fail_on = "hard_event"\n', encoding="utf-8")
    assert load_config(find_config()).fail_on is ConfidenceTier.HARD_EVENT
    nested = project / "traces"
    (nested / "tracelint.toml").write_text('format = "native"\n', encoding="utf-8")
    assert find_config(nested) == nested / "tracelint.toml"


def test_a_pyproject_without_the_table_is_passed_over(project):
    (project / "traces" / "pyproject.toml").write_text('[project]\nname = "x"\n', encoding="utf-8")
    _pyproject(project, '[tool.tracelint]\nfail_on = "hard_event"\n')
    assert find_config(project / "traces") == project / "pyproject.toml"


def test_discovery_stops_at_the_repository_root(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[tool.tracelint]\nformat = "otel"\n', encoding="utf-8"
    )
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    assert find_config(repo) is None


def test_flags_override_the_config(project):
    _pyproject(project, '[tool.tracelint]\nfail_on = "hard_event"\nrules = ["R3"]\n')
    assert _check("traces/retried.json") == 0  # only R3 runs: the R2a event is not looked for
    assert _check("traces/retried.json", "--rules", "R2a") == 1


def test_tools_are_relative_to_the_config_file(project, monkeypatch):
    _pyproject(project, '[tool.tracelint]\ntools = "tools.json"\n')
    monkeypatch.chdir(project / "traces")
    assert _check("refund.json") == 2  # found ../pyproject.toml, read ../tools.json


def test_an_explicit_config_file(project, tmp_path):
    other = tmp_path / "ci.toml"
    other.write_text('fail_on = "hard_event"\n', encoding="utf-8")
    assert _check("traces/retried.json", "--config", str(other)) == 1


# --- A mistake fails the run instead of loosening the gate ---------------------------------------


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ('[tool.tracelint]\nfail-on = "hard_event"\n', "unknown key 'fail-on'"),
        ('[tool.tracelint]\nfail_on = "warning"\n', "fail_on 'warning' is not one of"),
        ('[tool.tracelint]\nformat = "jaeger"\n', "format 'jaeger' is not one of"),
        ('[tool.tracelint]\nrules = ["R2"]\n', "unknown rule 'R2'"),
        ('[[tool.tracelint.ignore]]\nrule = "R3"\n', "needs a reason"),
        ('[[tool.tracelint.ignore]]\nrule = "R99"\nreason = "x"\n', "rule 'R99' is not a known"),
        (
            '[[tool.tracelint.ignore]]\nrule = "R3"\ntools = "x"\nreason = "x"\n',
            "unknown key 'tools'",
        ),
        ("[tool.tracelint]\nfail_on = \n", "not valid TOML"),
    ],
)
def test_an_invalid_config_is_an_input_error(project, capsys, body, message):
    _pyproject(project, body)
    assert _check("traces/retried.json") == 3
    assert message in capsys.readouterr().err


def test_load_config_names_a_missing_table(project):
    (project / "pyproject.toml").write_text('[project]\nname = "x"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match=r"no \[tool.tracelint\] table"):
        load_config(project / "pyproject.toml")


# --- What a finding is about ---------------------------------------------------------------------


def test_a_finding_key_ignores_where_the_finding_sits():
    def r2b(steps: list[int], value: str) -> Finding:
        evidence = {
            "step_indices": steps,
            "errored_tool": "run_release_pipeline",
            "consumer": "deploy",
            "consumed_values": [value],
        }
        return Finding("R2b", "error_mishandled", ConfidenceTier.HARD_DEFECT, "x", evidence)

    first, rerun = finding_key(r2b([3, 4], "b-4120")), finding_key(r2b([5, 9], "b-4121"))
    assert first == rerun
    assert first.tools == ("run_release_pipeline", "deploy")
    assert first.describe() == "R2b error_mishandled run_release_pipeline -> deploy"


def test_a_finding_key_reads_fields_and_signals():
    r1 = Finding(
        "R1",
        "schema_violation",
        ConfidenceTier.HARD_DEFECT,
        "x",
        {"tool": "refund", "errors": [{"path": "/amount", "keyword": "type", "message": "m"}]},
    )
    r3 = Finding(
        "R3", "hallucinated_arg", ConfidenceTier.CANDIDATE, "x", {"tool": "t", "field": "f"}
    )
    r2b = Finding(
        "R2b",
        "error_mishandled",
        ConfidenceTier.CANDIDATE,
        "x",
        {"tool": "t", "signal": "not_retried"},
    )
    assert finding_key(r1).fields == ("amount",)
    assert finding_key(r3).fields == ("f",)
    assert finding_key(r2b).signal == "not_retried"
