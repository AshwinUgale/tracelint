"""R13 — off-limits source: a call requested, or searched for, a source the contract rules out.

The operator declares the sources once (``off_limits`` beside ``tools``); a call names one through a
URL in its arguments or a search query. A benchmark's name anywhere else in a call (a task's own
data, a file header, a canary string) is not a request for it. Opt-in: no ``off_limits``, no output.
"""

from __future__ import annotations

import json

import pytest

from tracelint import ToolRegistry, build_trace, lint_trace
from tracelint.cli import main
from tracelint.findings import ARGS_UNKNOWN, ConfidenceTier
from tracelint.identity import finding_key
from tracelint.rules import rule_ids, select_rules
from tracelint.trace import Message, ResultStatus, Role, ToolCall, ToolResult

POLICY = {
    "sources": ["tbench.ai", "terminal-bench", "github.com/acme/bench-tasks"],
    "reason": "the benchmark publishes each task's solution",
}
REG = ToolRegistry.from_dict({"tools": {}, "off_limits": POLICY})


def _r13(*steps, registry=REG):
    return lint_trace(build_trace("t", list(steps)), select_rules(["R13"]), registry)


def _fetch(url, call_id="c1"):
    return ToolCall(call_id, "WebFetch", {"url": url, "prompt": "what is the answer?"})


def _shell(command, call_id="c1"):
    return ToolCall(call_id, "Bash", {"command": command})


def _ok(call_id="c1"):
    return ToolResult(call_id, "<html>...</html>", status=ResultStatus.OK)


def test_r13_is_registered():
    assert "R13" in rule_ids()


def test_without_a_policy_the_rule_adds_nothing():
    rep = _r13(_fetch("https://www.tbench.ai/registry/task"), _ok(), registry=ToolRegistry())
    assert rep.findings == [] and rep.coverage == []


def test_a_fetch_of_an_off_limits_url_is_a_hard_defect():
    rep = _r13(_fetch("https://www.tbench.ai/registry/terminal-bench-core/head/task"), _ok())
    (f,) = rep.active_findings
    assert f.rule == "R13" and f.finding_type == "off_limits_source"
    assert f.tier is ConfidenceTier.HARD_DEFECT and rep.exit_code == 2
    ev = f.evidence
    assert ev["tool"] == "WebFetch" and ev["field"] == "url" and ev["signal"] == "url"
    assert ev["source"] == "tbench.ai" and ev["outcome"] == "returned"
    assert ev["sources"] == ["tbench.ai", "terminal-bench"]  # the URL names both
    assert ev["reason"] == POLICY["reason"] and ev["step_indices"] == [0, 1]
    assert "requested" in f.summary and "it returned a result" in f.summary


def test_a_url_inside_a_shell_command_is_a_request():
    url = "https://huggingface.co/datasets/someone/terminal-bench-2-mirror/resolve/main/main.db"
    (f,) = _r13(_shell(f"curl -sL -o main.db {url}"), _ok()).active_findings
    assert f.evidence["field"] == "command" and f.evidence["source"] == "terminal-bench"
    assert f.evidence["matched"].startswith("https://huggingface.co/datasets/someone/")


def test_a_search_naming_a_source_is_a_hard_defect():
    call = ToolCall("c1", "WebSearch", {"query": 'tbench.ai "scandinavian" leaderboard answer'})
    (f,) = _r13(call, _ok()).active_findings
    assert f.evidence["signal"] == "query" and f.evidence["field"] == "query"
    assert "searched for" in f.summary and f.tier is ConfidenceTier.HARD_DEFECT


def test_a_list_of_queries_is_searched():
    call = ToolCall("c1", "web_search", {"queries": ["sqlite wal recovery", "terminal bench wal"]})
    (f,) = _r13(call, _ok()).active_findings
    assert f.evidence["matched"] == "terminal bench wal"  # a space reads like the declared '-'


@pytest.mark.parametrize(
    "args",
    [
        # A task whose own input is the benchmark's name: computing with it is not reaching it.
        {"command": "python -c \"print(model.encode(['terminal-bench']))\""},
        # Calendar headers and canary strings written into files.
        {"file_path": "/app/meeting.ics", "content": "PRODID:-//Terminal-Bench//Scheduler//EN"},
        {"content": "# BENCHMARK DATA SHOULD NEVER APPEAR. terminal-bench-canary GUID 26b5c67b"},
        # Looking for the name in local files.
        {"command": "grep -rn tbench.ai /app"},
    ],
)
def test_the_name_outside_a_url_or_a_query_is_not_a_request(args):
    assert _r13(ToolCall("c1", "Write", args), _ok()).active_findings == []


@pytest.mark.parametrize(
    "url",
    [
        "https://nottbench.ai/page",  # a whole token: tbench.ai must not be part of a longer name
        "https://example.com/my-terminal-benchmark",  # 'terminal-bench' inside 'terminal-benchmark'
        "https://github.com/acme/bench-tasksuite",  # a path source too
    ],
)
def test_a_source_matches_only_as_a_whole_token(url):
    assert _r13(_fetch(url), _ok()).active_findings == []


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/acme/bench-tasks/blob/main/task/solution.sh",
        "https://TBENCH.AI/registry",  # any case
        "https://www.google.com/search?q=terminal+bench+solution",  # a search by URL, decoded
        "https://example.org/terminal_bench/data",  # '_' reads like the declared '-'
        "https://example.org/x?ref=terminal%2Dbench",  # percent-encoded
    ],
)
def test_matching_forms(url):
    (f,) = _r13(_fetch(url), _ok()).active_findings
    assert f.rule == "R13"


def test_a_scheme_www_and_trailing_slash_in_a_declared_source_are_ignored():
    policy = {"sources": ["https://www.tbench.ai/"]}
    reg = ToolRegistry.from_dict({"tools": {}, "off_limits": policy})
    (f,) = _r13(_fetch("https://tbench.ai/registry"), _ok(), registry=reg).active_findings
    assert f.evidence["source"] == "https://www.tbench.ai/"


def test_punctuation_after_a_url_is_not_part_of_it():
    (f,) = _r13(_shell("echo see https://tbench.ai/task."), _ok()).active_findings
    assert f.evidence["matched"] == "https://tbench.ai/task"


def test_the_outcome_says_what_the_request_got_back():
    failed = _r13(_fetch("https://tbench.ai/x"), ToolResult("c1", "403", status=ResultStatus.ERROR))
    assert failed.active_findings[0].evidence["outcome"] == "failed"
    assert "it failed" in failed.active_findings[0].summary
    unrecorded = _r13(_fetch("https://tbench.ai/x"))
    assert unrecorded.active_findings[0].evidence["outcome"] == "unrecorded"
    assert unrecorded.active_findings[0].evidence["step_indices"] == [0]


def test_one_finding_per_call():
    rep = _r13(
        ToolCall("c1", "Bash", {"command": "curl https://tbench.ai/a https://tbench.ai/b"}),
        _ok(),
        _fetch("https://tbench.ai/c", call_id="c2"),
        _ok("c2"),
    )
    assert [f.evidence["call_id"] for f in rep.active_findings] == ["c1", "c2"]


def test_a_url_the_prompt_gave_is_a_possible_false_positive():
    url = "https://github.com/acme/bench-tasks/archive/main.zip"
    rep = _r13(Message(Role.USER, f"Download {url} and unpack it."), _fetch(url), _ok())
    (f,) = rep.active_findings
    assert f.possible_false_positive is True and f.evidence["in_prompt"] is True
    assert f.tier is ConfidenceTier.HARD_DEFECT  # still the declared source; the reader decides


def test_calls_with_unrecorded_arguments_are_disclosed_and_counted():
    hidden = ToolCall("c1", "web_search", {}, args_unavailable="arguments not captured")
    rep = _r13(hidden, _ok(), _fetch("https://example.com", call_id="c2"), _ok("c2"))
    assert rep.active_findings == []
    (s,) = rep.suppressions
    assert s.rule == "R13" and s.evidence["cause"] == ARGS_UNKNOWN
    (cov,) = rep.coverage
    assert (cov.evaluatable, cov.total) == (1, 2)


def test_the_baseline_key_names_the_tool_argument_and_surface():
    (f,) = _r13(_fetch("https://tbench.ai/x"), _ok()).active_findings
    key = finding_key(f)
    assert key.tools == ("WebFetch",) and key.fields == ("url",) and key.signal == "url"


# --- the contract --------------------------------------------------------------------------


def test_off_limits_loads_beside_tools_in_either_form():
    wrapped = ToolRegistry.from_dict({"tools": {"get": {}}, "off_limits": {"sources": ["x.io"]}})
    bare = ToolRegistry.from_dict({"get": {}, "off_limits": {"sources": ["x.io"]}})
    for reg in (wrapped, bare):
        assert reg.names() == ["get"]  # never read as a tool
        assert reg.off_limits is not None and reg.off_limits.sources == ("x.io",)
    assert ToolRegistry.from_dict({"tools": {}}).off_limits is None


@pytest.mark.parametrize(
    "policy, message",
    [
        (["tbench.ai"], "must be an object"),
        ({"sources": []}, "non-empty list"),
        ({"sources": ["ok", ""]}, "non-empty list"),
        ({"sources": "tbench.ai"}, "non-empty list"),
        ({"sources": ["ok"], "domains": ["x"]}, "unknown key"),
        ({"sources": ["ok"], "reason": 3}, "reason must be a string"),
    ],
)
def test_a_malformed_policy_is_an_error(policy, message):
    with pytest.raises(ValueError, match=message):
        ToolRegistry.from_dict({"tools": {}, "off_limits": policy})


def test_a_policy_with_no_tools_still_reaches_the_rule_from_the_cli(tmp_path, capsys):
    # A contract can be only a policy (a leaderboard checking submissions it has no schemas for).
    trace = build_trace("run", [_fetch("https://tbench.ai/registry/task"), _ok()])
    (tmp_path / "trace.json").write_text(trace.to_json(), encoding="utf-8")
    (tmp_path / "tools.json").write_text(json.dumps({"off_limits": POLICY}), encoding="utf-8")
    code = main(["check", str(tmp_path / "trace.json"), "--tools", str(tmp_path / "tools.json")])
    assert code == 2
    assert "R13" in capsys.readouterr().out


def test_a_malformed_policy_is_an_input_error_from_the_cli(tmp_path, capsys):
    trace = build_trace("run", [_fetch("https://tbench.ai/x"), _ok()])
    (tmp_path / "trace.json").write_text(trace.to_json(), encoding="utf-8")
    tools = tmp_path / "tools.json"
    tools.write_text(json.dumps({"off_limits": {"sources": []}}), encoding="utf-8")
    assert main(["check", str(tmp_path / "trace.json"), "--tools", str(tools)]) == 3
    assert "off_limits.sources" in capsys.readouterr().err
