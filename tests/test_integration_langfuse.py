"""Langfuse integration — fetch, lint, and write findings back as Scores.

Exercised against a fake client (the SDK seam), so no live Langfuse is needed. The one thing only
a real project can prove — that the scores actually render beside a trace — is the manual
screenshot step; everything mechanical is pinned here.
"""

from __future__ import annotations

import json
import types

from tracelint.cli import main
from tracelint.integrations.langfuse import LangfuseIntegration
from tracelint.tools import ToolRegistry

# A trace with two planted, *certain* defects: get_order errors, its value A100 is reused by a
# side-effecting refund_order (R2b hard_defect), which then runs twice (R8 hard_event).
TRACE = {
    "id": "trace-xyz",
    "input": "Refund order A100.",
    "output": "Your refund is processed.",
    "observations": [
        {
            "id": "o1", "type": "tool", "name": "get_order",
            "input": {"order_id": "A100"},
            "output": {"order_id": "A100", "status": "error"},
            "level": "ERROR", "statusMessage": "500", "startTime": "2024-01-01T00:00:01Z",
        },
        {
            "id": "o2", "type": "tool", "name": "refund_order",
            "input": {"order_id": "A100"}, "output": {"refunded": True},
            "startTime": "2024-01-01T00:00:02Z",
        },
        {
            "id": "o3", "type": "tool", "name": "refund_order",
            "input": {"order_id": "A100"}, "output": {"refunded": True},
            "startTime": "2024-01-01T00:00:03Z",
        },
    ],
}

REG = ToolRegistry.from_dict(
    {
        "tools": {
            "get_order": {},
            "refund_order": {
                "metadata": {
                    "side_effecting": True,
                    "failure_when": {"pointer": "/refunded", "equals": False},
                }
            },
        }
    }
)


class FakeClient:
    """Stands in for a Langfuse SDK client: serves one trace, records create_score calls."""

    def __init__(self, trace):
        self._trace = trace
        self.created: list[dict] = []
        self.api = types.SimpleNamespace(
            trace=types.SimpleNamespace(get=lambda trace_id: self._trace)
        )

    def create_score(self, **kwargs):
        self.created.append(kwargs)


def _integration():
    return LangfuseIntegration(client=FakeClient(TRACE))


def test_read_only_by_default():
    integ = _integration()
    result = integ.check("trace-xyz", registry=REG)
    assert result.written == 0
    assert integ.client().created == []  # nothing written without --write-back
    assert result.report.has_hard_defect


def test_headline_trace_scores():
    plans = _integration().check("trace-xyz", registry=REG).plans
    by_name = {p.name: p for p in plans}
    assert by_name["tracelint.passed"].value == 0
    assert by_name["tracelint.passed"].data_type == "BOOLEAN"
    assert by_name["tracelint.hard_defects"].value >= 1
    assert by_name["tracelint.hard_defects"].data_type == "NUMERIC"


def test_findings_attach_to_offending_observation_with_evidence():
    plans = _integration().check("trace-xyz", registry=REG).plans
    finding_plans = [p for p in plans if p.observation_id]
    assert finding_plans, "expected observation-level finding scores"
    for p in finding_plans:
        assert p.observation_id in {"o1", "o2", "o3"}
        assert p.comment  # evidence text present
        assert p.data_type == "BOOLEAN"


def test_only_hard_tiers_written_back():
    result = _integration().check("trace-xyz", registry=REG)
    finding_plans = [
        p for p in result.plans
        if p.name not in ("tracelint.passed", "tracelint.hard_defects")
    ]
    hard = [
        f for f in result.report.active_findings
        if f.tier.value in ("hard_defect", "hard_event")
    ]
    assert len(finding_plans) == len(hard) >= 1  # candidates are review-only, never written


def test_write_back_calls_create_score():
    integ = _integration()
    result = integ.check("trace-xyz", registry=REG, write_back=True)
    created = integ.client().created
    assert result.written == len(result.plans) == len(created)
    for kw in created:
        assert kw["score_id"] and kw["trace_id"] == "trace-xyz"


def test_score_ids_are_stable_across_runs():
    first = _integration().check("trace-xyz", registry=REG, write_back=True)
    second = _integration().check("trace-xyz", registry=REG, write_back=True)
    ids1 = sorted(p.score_id for p in first.plans)
    ids2 = sorted(p.score_id for p in second.plans)
    assert ids1 == ids2  # idempotent keys → re-run updates, not duplicates


# --- CLI wiring -----------------------------------------------------------------------

def _write_tools(tmp_path):
    p = tmp_path / "tools.json"
    p.write_text(
        json.dumps(
            {
                "tools": {
                    "get_order": {},
                    "refund_order": {
                        "metadata": {
                            "side_effecting": True,
                            "failure_when": {"pointer": "/refunded", "equals": False},
                        }
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    return str(p)


def test_cli_dry_run(monkeypatch, capsys, tmp_path):
    import tracelint.integrations.langfuse as lf

    client = FakeClient(TRACE)
    monkeypatch.setattr(lf, "_default_client", lambda: client)
    code = main(["langfuse", "check", "--trace", "trace-xyz", "--tools", _write_tools(tmp_path)])
    out = capsys.readouterr().out
    assert code == 2
    assert client.created == []  # dry run writes nothing
    assert "would write these scores" in out
    assert "tracelint.passed" in out


def test_cli_write_back(monkeypatch, capsys, tmp_path):
    import tracelint.integrations.langfuse as lf

    client = FakeClient(TRACE)
    monkeypatch.setattr(lf, "_default_client", lambda: client)
    code = main(
        ["langfuse", "check", "--trace", "trace-xyz", "--tools", _write_tools(tmp_path),
         "--write-back"]
    )
    out = capsys.readouterr().out
    assert code == 2
    assert client.created  # scores written
    assert "wrote" in out


def test_vendor_error_becomes_clean_exit_3(monkeypatch, capsys):
    """A Langfuse auth/network error surfaces as exit 3 with guidance, not a raw traceback."""
    import tracelint.integrations.langfuse as lf

    class Unauthorized(Exception):
        pass

    class BadClient:
        def __init__(self):
            self.api = types.SimpleNamespace(
                trace=types.SimpleNamespace(
                    get=lambda _tid: (_ for _ in ()).throw(Unauthorized("401 Invalid credentials"))
                )
            )

    monkeypatch.setattr(lf, "_default_client", BadClient)
    code = main(["langfuse", "check", "--trace", "trace-xyz"])
    assert code == 3
    err = capsys.readouterr().err
    assert "Langfuse" in err and "region" in err


def test_missing_sdk_is_a_clean_error(monkeypatch, capsys):
    """No Langfuse SDK installed -> exit 3 with a helpful message, never a traceback."""
    import tracelint.integrations.langfuse as lf

    def _boom():
        raise RuntimeError('install it with `pip install "tracelint[langfuse]"`')

    monkeypatch.setattr(lf, "_default_client", _boom)
    code = main(["langfuse", "check", "--trace", "trace-xyz"])
    assert code == 3
    assert "tracelint[langfuse]" in capsys.readouterr().err
