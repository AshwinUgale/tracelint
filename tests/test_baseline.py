"""The CI baseline: accept the findings a project has today, fail on new ones and on lost coverage.

A team adopting tracelint on traces that already have findings had two choices: a red build until
every finding was fixed, or turning the rules off. Now ``--update-baseline`` records what the
traces show, and later runs accept up to that many of each finding and fail on anything beyond.

Findings are matched by trace file and :class:`~tracelint.identity.FindingKey` with a count, never
by step position or value, so a re-run of the agent that shifts its steps still matches. The
baseline also records what each rule could check, so a run that checks less (content capture off,
schemas deleted) fails instead of going quietly green.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tracelint import build_trace
from tracelint.baseline import Baseline
from tracelint.cli import main
from tracelint.trace import Message, ResultStatus, Role, ToolCall, ToolResult

SIDE_EFFECTING = {"metadata": {"side_effecting": True}}
TOOLS = {"tools": {"refund_order": SIDE_EFFECTING, "send_email": SIDE_EFFECTING}}
SCHEMAS = {
    "tools": {
        "get_order": {"schema": {"type": "object", "properties": {"order_id": {"type": "string"}}}},
        "refund_order": {
            "schema": {"type": "object", "properties": {"order_id": {"type": "string"}}},
            **SIDE_EFFECTING,
        },
    }
}


def _refund_steps(order: str, card: str, *, args_known: bool = True) -> list:
    """A lookup fails but returns a cached card, and the refund goes to it (an R2b hard defect)."""
    failed = {"order_id": order, "status": "error", "payment_method": card}
    refund = ToolCall(f"r-{order}", "refund_order", {"order_id": order, "payment_method": card})
    if not args_known:
        refund = ToolCall(f"r-{order}", "refund_order", {}, args_unavailable="redacted")
    return [
        ToolCall(f"g-{order}", "get_order", {"order_id": order}),
        ToolResult(f"g-{order}", failed, status=ResultStatus.ERROR),
        refund,
        ToolResult(f"r-{order}", {"refunded": True}, status=ResultStatus.OK),
    ]


def _trace(*, orders=(("A100", "pm_7731"),), shift=0, extra=(), args_known=True):
    steps: list = [Message(Role.USER, "Refund my orders to the card on file.")]
    steps += [Message(Role.SYSTEM, f"note {i}") for i in range(shift)]  # moves every step index
    for order, card in orders:
        steps += _refund_steps(order, card, args_known=args_known)
    steps += list(extra)
    return build_trace("run", steps)


def _email(card: str, tool: str = "send_email") -> list:
    return [
        ToolCall("e1", tool, {"to": "ann@example.com", "card": card}),
        ToolResult("e1", {"sent": True}, status=ResultStatus.OK),
    ]


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    (tmp_path / "traces").mkdir()
    (tmp_path / "tools.json").write_text(json.dumps(TOOLS), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _write(repo: Path, trace, name: str = "refund.json") -> str:
    (repo / "traces" / name).write_text(trace.to_json(), encoding="utf-8")
    return f"traces/{name}"


def _check(*args: str) -> int:
    return main(["check", "--tools", "tools.json", "--baseline", "baseline.json", *args])


def _record(*paths: str) -> None:
    assert _check(*paths, "--update-baseline") == 0


# --- Accept today's findings, fail on new ones ---------------------------------------------------


def test_recorded_findings_pass_and_stay_visible(repo, capsys):
    path = _write(repo, _trace())
    assert main(["check", path, "--tools", "tools.json"]) == 2  # without the baseline
    _record(path)
    capsys.readouterr()

    assert _check(path) == 0
    out = capsys.readouterr().out
    assert "ignored (1)" in out and "in the baseline" in out


def test_a_rerun_with_shifted_steps_and_new_values_still_matches(repo):
    path = _write(repo, _trace())
    _record(path)
    _write(repo, _trace(orders=(("A207", "pm_9902"),), shift=3))  # same defect, new run
    assert _check(path) == 0


def test_more_of_a_finding_than_recorded_fails(repo, capsys):
    path = _write(repo, _trace())
    _record(path)
    _write(repo, _trace(orders=(("A100", "pm_7731"), ("B200", "pm_5521"))))
    assert _check(path) == 2
    out = capsys.readouterr().out
    assert "ignored (1)" in out  # one accepted, the second is new
    assert "[hard_defect] R2b" in out


def test_a_new_side_effect_of_the_same_failure_fails(repo, capsys):
    path = _write(repo, _trace())
    _record(path)
    _write(repo, _trace(extra=_email("pm_7731")))  # the stale card now also reaches an email
    assert _check(path) == 2
    assert "1 accepted finding(s) no longer occur" in capsys.readouterr().err


def test_a_harmless_new_use_of_the_same_failure_does_not(repo):
    path = _write(repo, _trace())
    _record(path)
    _write(repo, _trace(extra=_email("pm_7731", tool="log_event")))  # not side-effecting
    assert _check(path) == 0


def test_findings_below_the_gate_are_not_recorded(repo):
    path = _write(repo, _trace())
    _record(path)
    recorded = json.loads((repo / "baseline.json").read_text(encoding="utf-8"))
    (entry,) = recorded["files"]["traces/refund.json"]["findings"]
    assert entry["rule"] == "R2b" and entry["tier"] == "hard_defect"  # the R2a event is not kept


def test_an_accepted_finding_covers_a_milder_one_but_not_a_worse_one(repo):
    # An echo makes the R2b use a candidate; without the echo the same key is a hard defect.
    echo = [
        ToolCall("v1", "verify_card", {"card": "pm_7731"}),
        ToolResult("v1", {"card": "pm_7731", "valid": True}, status=ResultStatus.OK),
    ]
    steps = [Message(Role.USER, "Refund A100.")]
    steps += _refund_steps("A100", "pm_7731")[:2] + echo + _refund_steps("A100", "pm_7731")[2:]
    milder = build_trace("run", steps)

    gate = ("--fail-on", "candidate", "--rules", "R2b")
    path = _write(repo, milder)
    assert _check(path, *gate, "--update-baseline") == 0
    _write(repo, _trace())  # the echo is gone: a hard defect now
    assert _check(path, *gate) == 2

    assert _check(path, *gate, "--update-baseline") == 0  # a recorded hard defect...
    _write(repo, milder)
    assert _check(path, *gate) == 0  # ...covers the candidate


def test_an_accepted_finding_that_no_longer_occurs_is_noted(repo, capsys):
    path = _write(repo, _trace())
    _record(path)
    _write(repo, build_trace("run", [Message(Role.USER, "hi")]))
    capsys.readouterr()
    assert _check(path) == 0
    assert "1 accepted finding(s) no longer occur" in capsys.readouterr().err


def test_a_trace_file_the_baseline_never_saw_is_checked_in_full(repo):
    _record(_write(repo, _trace()))
    other = _write(repo, _trace(args_known=False, orders=(("C3", "pm_1"),)), "other.json")
    assert _check(other) == 0  # no finding to accept, no coverage to compare
    other = _write(repo, _trace(), "other.json")
    assert _check(other) == 2


# --- The coverage ratchet ------------------------------------------------------------------------


def test_a_tool_a_rule_can_no_longer_check_fails_the_run(repo, capsys):
    path = _write(repo, _trace())
    _record(path)
    _write(repo, _trace(args_known=False))  # the exporter now redacts the refund's arguments
    capsys.readouterr()
    assert _check(path) == 1
    out = capsys.readouterr().out
    assert "checks less than the baseline" in out
    assert "R3 can no longer check 'refund_order'" in out


def test_a_rule_that_stopped_checking_anything_fails_the_run(repo, capsys):
    (repo / "tools.json").write_text(json.dumps(SCHEMAS), encoding="utf-8")
    path = _write(repo, _trace())
    _record(path)
    (repo / "tools.json").write_text(json.dumps(TOOLS), encoding="utf-8")  # schemas deleted
    capsys.readouterr()
    assert _check(path) == 1
    assert "R1 checked 0/2 tool calls" in capsys.readouterr().out


def test_the_ratchet_can_be_turned_off(repo):
    path = _write(repo, _trace())
    _record(path)
    _write(repo, _trace(args_known=False))
    assert _check(path, "--no-ratchet") == 0
    (repo / "pyproject.toml").write_text("[tool.tracelint]\nratchet = false\n", encoding="utf-8")
    assert _check(path) == 0


def test_gate_failures_are_in_the_json(repo, tmp_path):
    path = _write(repo, _trace())
    _record(path)
    _write(repo, _trace(args_known=False))
    out = tmp_path / "out.json"
    assert _check(path, "--json", str(out)) == 1
    report = json.loads(out.read_text(encoding="utf-8"))["reports"][0]
    assert report["exit_code"] == 1
    assert any("can no longer check 'refund_order'" in g for g in report["gate_failures"])


# --- The file ------------------------------------------------------------------------------------


def test_updating_from_some_traces_keeps_the_others(repo):
    first, second = _write(repo, _trace(), "a.json"), _write(repo, _trace(), "b.json")
    _record(first, second)
    _write(repo, build_trace("run", [Message(Role.USER, "hi")]), "a.json")
    _record(first)
    files = json.loads((repo / "baseline.json").read_text(encoding="utf-8"))["files"]
    assert files["traces/a.json"]["findings"] == []
    assert files["traces/b.json"]["findings"]  # untouched


def test_paths_are_relative_to_the_baseline_file(repo, monkeypatch):
    path = _write(repo, _trace())
    _record(path)
    monkeypatch.chdir(repo / "traces")
    assert (
        main(["check", "refund.json", "--tools", "../tools.json", "--baseline", "../baseline.json"])
        == 0
    )


def test_the_config_names_the_baseline(repo, monkeypatch):
    (repo / "ci").mkdir()
    (repo / "ci" / "tracelint.toml").write_text(
        'tools = "../tools.json"\nbaseline = "accepted.json"\n', encoding="utf-8"
    )
    path = _write(repo, _trace())
    assert main(["check", path, "--config", "ci/tracelint.toml", "--update-baseline"]) == 0
    assert (repo / "ci" / "accepted.json").is_file()
    assert main(["check", path, "--config", "ci/tracelint.toml"]) == 0


def test_the_file_is_stable_and_round_trips(repo):
    path = _write(repo, _trace(orders=(("A100", "pm_7731"), ("B200", "pm_5521"))))
    _record(path)
    first = (repo / "baseline.json").read_text(encoding="utf-8")
    _record(path)
    assert (repo / "baseline.json").read_text(encoding="utf-8") == first
    loaded = Baseline.load(repo / "baseline.json")
    loaded.save()
    assert (repo / "baseline.json").read_text(encoding="utf-8") == first
    (entry,) = json.loads(first)["files"]["traces/refund.json"]["findings"]
    assert entry["count"] == 2


# --- Mistakes are input errors -------------------------------------------------------------------


def test_a_missing_baseline_is_an_input_error(repo, capsys):
    assert _check(_write(repo, _trace())) == 3
    assert "--update-baseline" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("not json", "not a tracelint baseline"),
        ('{"version": 9, "files": {}}', "unsupported baseline version 9"),
        ('{"version": 1}', "not a tracelint baseline"),
    ],
)
def test_an_invalid_baseline_is_an_input_error(repo, capsys, content, message):
    (repo / "baseline.json").write_text(content, encoding="utf-8")
    assert _check(_write(repo, _trace())) == 3
    assert message in capsys.readouterr().err


def test_updating_needs_a_baseline_file(repo, capsys):
    assert main(["check", _write(repo, _trace()), "--update-baseline"]) == 3
    assert "needs a baseline file" in capsys.readouterr().err


def test_ratchet_must_be_a_boolean(repo, capsys):
    (repo / "pyproject.toml").write_text('[tool.tracelint]\nratchet = "no"\n', encoding="utf-8")
    assert _check(_write(repo, _trace())) == 3
    assert "ratchet must be true or false" in capsys.readouterr().err
