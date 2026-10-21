"""The Inspect Scout scanners on Scout's own objects, through its registry and ``scan()``.

Needs ``pip install "tracelint[scout]"`` (Python 3.11+); skipped otherwise. CI runs it in the job
that installs the extra.
"""

from __future__ import annotations

import asyncio
import json
from importlib.metadata import entry_points
from pathlib import Path

import pytest

pytest.importorskip("inspect_scout")

from inspect_scout import (  # noqa: E402
    Result,
    Transcript,
    scan,
    scan_results_df,
    transcripts_db,
    transcripts_from,
)

from tracelint.integrations.scout.scanners import (  # noqa: E402
    malformed_arguments,
    record_quality,
    redundant_calls,
    tool_errors,
    tool_loops,
    tracelint,
)

FIXTURES = Path(__file__).parent / "fixtures" / "scout"
TOOLS = FIXTURES / "tools.json"
SCANNERS = (
    "tracelint",
    "tool_errors",
    "tool_loops",
    "redundant_calls",
    "malformed_arguments",
    "record_quality",
)


@pytest.fixture
def transcript() -> Transcript:
    return Transcript.model_validate_json((FIXTURES / "transcript.json").read_text("utf-8"))


def _run(scanner, transcript):
    return asyncio.run(scanner(transcript))


def test_tracelint_returns_one_labelled_result_per_rule(transcript):
    results = _run(tracelint(), transcript)
    assert all(isinstance(r, Result) for r in results)
    assert [(r.label, r.value) for r in results] == [
        ("tool_error_event", 4),
        ("loop", 1),
        ("redundant_call", 1),
        ("malformed_arguments", 1),
    ]
    loop = results[1]
    # citations link the exact messages, Scout-style
    assert [(ref.type, ref.cite, ref.id) for ref in loop.references] == [
        ("message", "[M5]", "m-a2"),
        ("message", "[M7]", "m-a3"),
        ("message", "[M9]", "m-a4"),
    ]
    assert loop.explanation.startswith("[M5] [M7] [M9] (candidate) 'search_orders' called 3 times")
    assert loop.metadata["rule"] == "R4"


def test_tracelint_with_a_contract_and_rule_selection(transcript):
    by = {r.label: r for r in _run(tracelint(tools=str(TOOLS)), transcript)}
    assert by["off_limits_source"].value == 1
    assert [ref.id for ref in by["off_limits_source"].references] == ["m-a6"]
    # the CLI hands -S rules=R4,R13 over as a list, -S rules=R4 as a string
    picked = _run(tracelint(rules=["R4", "R13"], tools=str(TOOLS)), transcript)
    assert [r.label for r in picked] == ["loop", "off_limits_source"]
    assert [r.label for r in _run(tracelint(rules="R6"), transcript)] == ["malformed_arguments"]


@pytest.mark.parametrize(
    ("factory", "value", "cites"),
    [
        (tool_errors, 4, ["[M4]", "[M12]", "[M13]", "[M14]"]),
        (tool_loops, 1, ["[M5]", "[M7]", "[M9]"]),
        (redundant_calls, 1, ["[M5]", "[M9]"]),
        (malformed_arguments, 1, ["[M11]"]),
    ],
)
def test_single_rule_scanners_return_one_unlabelled_result(transcript, factory, value, cites):
    result = _run(factory(), transcript)
    assert isinstance(result, Result)
    assert result.label is None
    assert result.value == value
    assert [ref.cite for ref in result.references] == cites


def test_min_tier(transcript):
    assert _run(tool_errors(min_tier="hard_event"), transcript).value == 3
    assert _run(tool_loops(min_tier="hard_event"), transcript).value == 0


def _incomplete_record() -> Transcript:
    path = FIXTURES / "incomplete_record.json"
    return Transcript.model_validate_json(path.read_text("utf-8"))


def test_record_quality_on_scouts_objects(transcript):
    clean = _run(record_quality(), transcript)
    assert (clean.value, clean.label, clean.references) == ([], None, [])

    record = _run(record_quality(), _incomplete_record())
    assert record.value == [
        "results_missing",
        "arguments_missing",
        "placeholder_turns",
        "ends_unanswered",
        "subagents_missing",  # c3: its tool event points at an agent span that is not there
        "duplicate_call_ids",
        "redactions",
    ]
    assert [(r.cite, r.id) for r in record.references] == [
        ("[M2]", "a1"),
        ("[M4]", "a2"),
        ("[M7]", "a3"),
        ("[M9]", "a4"),
        ("[M10]", "a5"),
    ]
    assert record.metadata["heuristic"] == ["placeholder_turns", "subagents_missing"]
    assert record.metadata["subagent_runs"] == 1


def test_bad_arguments_fail_when_the_scanner_is_created():
    with pytest.raises(ValueError, match="unknown rule"):
        tracelint(rules=["R99"])
    with pytest.raises(ValueError):
        tool_loops(min_tier="severe")
    with pytest.raises(FileNotFoundError):
        tool_errors(tools="no-such-tools.json")


def test_a_message_without_an_id_still_gets_a_reference():
    transcript = Transcript.model_validate(
        {
            "transcript_id": "no-ids",
            "messages": [
                {"role": "assistant", "content": "", "tool_calls": [
                    {"id": "c1", "function": "f", "arguments": {}}
                ]},
                {"role": "tool", "tool_call_id": "c1", "content": "",
                 "error": {"type": "unknown", "message": "boom"}},
            ],
        }
    )
    for message in transcript.messages:
        message.id = None  # as in a log written before Inspect assigned message ids
    (ref,) = _run(tool_errors(), transcript).references
    assert ref.cite == "[M2]" and ref.id


def test_the_scanners_are_registered_under_the_tracelint_namespace():
    if not any(ep.name == "tracelint" for ep in entry_points(group="inspect_ai")):
        pytest.skip("tracelint is not installed (its inspect_ai entry point is not registered)")
    from inspect_ai._util.registry import registry_lookup

    for name in SCANNERS:
        assert callable(registry_lookup("scanner", f"tracelint/{name}")), name


def test_scan_end_to_end(transcript, tmp_path):
    async def write() -> None:
        async with transcripts_db(str(tmp_path / "db")) as db:
            await db.insert([transcript, _incomplete_record()])

    asyncio.run(write())
    status = scan(
        scanners=[tracelint(tools=str(TOOLS)), tool_loops(), record_quality()],
        transcripts=transcripts_from(str(tmp_path / "db")),
        scans=str(tmp_path / "scans"),
        display="none",
    )
    assert status.complete
    frames = scan_results_df(status.location).scanners
    rules = frames["tracelint"]
    rules = rules[rules["transcript_id"] == transcript.transcript_id].set_index("label")
    assert int(rules.loc["off_limits_source", "value"]) == 1
    assert rules.loc["loop", "metadata.rule"] == "R4"  # Scout spreads a result set's metadata
    assert frames["tracelint"]["scan_error"].isna().all()
    loops = frames["tool_loops"]
    (loop,) = loops[loops["transcript_id"] == transcript.transcript_id].itertuples()
    assert loop.value == 1
    assert [r["id"] for r in json.loads(loop.message_references)] == ["m-a2", "m-a3", "m-a4"]
    records = frames["record_quality"].set_index("transcript_id")
    assert records["scan_error"].isna().all()
    assert set(records["value_type"]) == {"array"}  # the data frame keeps the list as JSON
    assert json.loads(records.loc[transcript.transcript_id, "value"]) == []
    issues = json.loads(records.loc["synthetic-incomplete-record", "value"])
    assert "ends_unanswered" in issues and "subagents_missing" in issues
