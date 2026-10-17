"""Offline tests for the engineer's code-built before/after timeline."""

import os

os.environ.setdefault("TYPESAFE_API_KEY", "test-key")

from backend.pipeline import graph
from backend.pipeline.answers import render_appendix, serialize_answer
from backend.pipeline.contracts import AnswerOutput
from backend.pipeline.timeline import (
    DERIVED_TOOL_NAME,
    MAX_ROWS,
    build_timeline,
    timeline_record,
    with_timeline,
)
from ui.rendering import canonicalize


def _row(record_id, ts, message="m", severity="INFO", service="nova.compute", **extra):
    return {
        "record_id": record_id, "ts": ts, "severity": severity, "service": service,
        "entity_id": "2931", "message": message, **extra,
    }


def _rec(rid, kind, rows, tool, dataset="openstack", **extra):
    return {
        "id": rid, "tool_name": tool, "arguments": {}, "row_count": len(rows),
        "rows": rows, "kind": kind, "dataset": dataset, "query": "SELECT 1",
        "filters": {}, "truncated": False, **extra,
    }


def _ts(second, micro=0):
    return f"2017-05-16 00:00:{second:02d}.{micro:06d}+00:00"


# A search hit on line 56 and its two neighbours fetched by line id.
HIT = _rec(
    "R1", "search",
    [_row(56, _ts(10), "Instance failed to spawn", "ERROR")],
    "search_logs_tool",
)
BEFORE = _rec("R2", "lookup", [_row(55, _ts(8, 500000), "Claim successful")],
              "get_log_by_line_id_tool")
AFTER = _rec("R3", "lookup", [_row(57, _ts(12), "Deleting instance")],
             "get_log_by_line_id_tool")


# --- consecutive lines (the fallback when no nearby-events result exists) ---------


def test_neighbours_around_a_search_hit_form_an_ordered_timeline():
    timeline = build_timeline([HIT, BEFORE, AFTER])
    assert timeline["anchor_record_id"] == 56
    assert timeline["dataset"] == "openstack"
    assert [r["record_id"] for r in timeline["rows"]] == [55, 56, 57]
    assert [r["offset"] for r in timeline["rows"]] == [-1, 0, 1]
    assert [r["seconds"] for r in timeline["rows"]] == [-1.5, 0.0, 2.0]
    assert [r["from"] for r in timeline["rows"]] == ["R2", "R1", "R3"]
    assert timeline["source_ids"] == ["R1", "R2", "R3"]


def test_the_order_of_the_tool_calls_does_not_matter():
    shuffled = build_timeline([AFTER, HIT, BEFORE])
    assert [r["record_id"] for r in shuffled["rows"]] == [55, 56, 57]
    assert shuffled["anchor_record_id"] == 56


def test_without_a_search_hit_the_middle_line_is_the_anchor():
    # The user named line 56 directly; the agent looked it up and its neighbours.
    middle = _rec("R1", "lookup", [_row(56, _ts(10))], "get_log_by_line_id_tool")
    timeline = build_timeline([middle, BEFORE, AFTER])
    assert timeline["anchor_record_id"] == 56
    assert [r["offset"] for r in timeline["rows"]] == [-1, 0, 1]


def test_lines_that_are_not_consecutive_are_not_a_timeline():
    far = _rec("R2", "lookup", [_row(900, _ts(30))], "get_log_by_line_id_tool")
    assert build_timeline([HIT, far]) is None


def test_the_longest_run_wins_and_ties_go_to_the_first_seen():
    other = _rec("R4", "search", [_row(200, _ts(40))], "search_logs_tool")
    other_next = _rec("R5", "lookup", [_row(201, _ts(41))], "get_log_by_line_id_tool")
    longest = build_timeline([other, other_next, HIT, BEFORE, AFTER])
    assert longest["anchor_record_id"] == 56
    tie = build_timeline([other, other_next, HIT, AFTER])
    assert tie["anchor_record_id"] == 200  # first seen of two equal runs


def test_a_single_row_is_not_a_timeline():
    assert build_timeline([HIT]) is None
    assert with_timeline([HIT]) == [HIT]


def test_lines_of_different_datasets_are_never_mixed():
    other_dataset = _rec("R2", "lookup", [_row(57, _ts(12))],
                         "get_log_by_line_id_tool", dataset="bgl")
    assert build_timeline([HIT, other_dataset]) is None


def test_rows_without_a_timestamp_or_id_are_ignored():
    no_ts = _rec("R2", "lookup", [{"record_id": 57, "message": "x"}],
                 "get_log_by_line_id_tool")
    no_id = _rec("R3", "lookup", [{"ts": _ts(1), "message": "x"}],
                 "get_log_by_line_id_tool")
    assert build_timeline([HIT, no_ts, no_id]) is None


def test_counts_and_trends_never_feed_the_timeline():
    count = _rec("R2", "aggregate", [_row(57, _ts(12))], "count_by_category_tool")
    assert build_timeline([HIT, count]) is None


def test_the_legacy_column_names_still_work():
    legacy = [
        _rec("R1", "search", [{"line_id": 10, "log_timestamp": "2017-05-16 00:00:01",
                               "level": "ERROR", "component": "c", "content": "boom"}],
             "search_logs_tool"),
        _rec("R2", "lookup", [{"line_id": 11, "log_timestamp": "2017-05-16 00:00:03",
                               "level": "INFO", "component": "c", "content": "after"}],
             "get_log_by_line_id_tool"),
    ]
    timeline = build_timeline(legacy)
    assert [r["seconds"] for r in timeline["rows"]] == [0.0, 2.0]
    assert timeline["rows"][0]["severity"] == "ERROR"
    assert timeline["rows"][1]["message"] == "after"


def test_timestamps_with_mixed_offsets_give_no_seconds_instead_of_failing():
    naive = _rec("R2", "lookup", [_row(57, "2017-05-16 00:00:12")],
                 "get_log_by_line_id_tool")
    timeline = build_timeline([HIT, naive])
    assert [r["seconds"] for r in timeline["rows"]] == [0.0, None]


def test_a_long_message_is_cut_with_an_explicit_marker():
    long_hit = _rec("R1", "search", [_row(56, _ts(10), "x" * 1000)], "search_logs_tool")
    timeline = build_timeline([long_hit, AFTER])
    message = timeline["rows"][0]["message"]
    assert message.endswith("…[cut]") and len(message) < 300


# --- nearby-events results (get_nearby_events_tool) --------------------------------


def _context(rows, **extra):
    return _rec("R1", "context", rows, "get_nearby_events_tool", **extra)


def test_a_nearby_events_result_is_used_with_its_own_offsets():
    rows = [
        _row(54, _ts(5), offset=-2, request_id="req-a"),
        _row(56, _ts(10), offset=0, request_id="req-a"),
        _row(55, _ts(8), offset=-1, request_id="req-b"),
        _row(58, _ts(20), offset=2, request_id="req-a"),
    ]
    timeline = build_timeline([_context(rows)])
    assert [r["record_id"] for r in timeline["rows"]] == [54, 55, 56, 58]
    assert [r["offset"] for r in timeline["rows"]] == [-2, -1, 0, 2]
    assert [r["seconds"] for r in timeline["rows"]] == [-5.0, -2.0, 0.0, 10.0]
    assert [r["same_request"] for r in timeline["rows"]] == [True, False, True, True]
    assert timeline["source_ids"] == ["R1"]


def test_a_nearby_events_result_wins_over_consecutive_lines():
    context = _context([_row(10, _ts(1), offset=-1), _row(11, _ts(2), offset=0)])
    timeline = build_timeline([HIT, BEFORE, AFTER, context])
    assert timeline["anchor_record_id"] == 11
    assert timeline["source_ids"] == ["R1"]


def test_a_nearby_events_result_for_an_unknown_line_gives_no_timeline():
    assert build_timeline([_context([])]) is None


def test_without_an_anchor_row_a_nearby_events_result_falls_back_to_runs():
    rows = [_row(54, _ts(5), offset=-2), _row(55, _ts(8), offset=-1)]
    timeline = build_timeline([_context(rows)])
    # No offset-0 row: the consecutive-line path still finds lines 54-55.
    assert [r["record_id"] for r in timeline["rows"]] == [54, 55]


def test_a_very_long_context_keeps_the_lines_nearest_the_anchor():
    rows = [_row(1000 + i, _ts(i % 60), offset=i - 60) for i in range(121)]
    timeline = build_timeline([_context(rows)])
    assert len(timeline["rows"]) == MAX_ROWS
    offsets = [r["offset"] for r in timeline["rows"]]
    assert 0 in offsets and max(abs(o) for o in offsets) <= 50
    assert timeline["capped"] is True


# --- the derived record ------------------------------------------------------


def test_the_derived_record_follows_the_priority_record_pattern():
    once = with_timeline([HIT, BEFORE, AFTER])
    twice = with_timeline(once)
    assert [r["id"] for r in once] == ["R1", "R2", "R3", "R4"]
    assert [r["id"] for r in twice] == ["R1", "R2", "R3", "R4"]
    derived = once[-1]
    assert derived["tool_name"] == DERIVED_TOOL_NAME and derived["kind"] == "derived"
    assert derived["source_ids"] == ["R1", "R2", "R3"]
    assert derived["measures_frequency"] is False
    assert derived["row_count"] == 3 and derived["truncated"] is False


def test_the_record_is_rebuilt_when_the_evidence_changes():
    first = with_timeline([HIT, BEFORE])
    assert [r["record_id"] for r in first[-1]["rows"]] == [55, 56]
    second = with_timeline([*first, AFTER])
    assert [r["id"] for r in second] == ["R1", "R2", "R3", "R4"]
    assert [r["record_id"] for r in second[-1]["rows"]] == [55, 56, 57]


def test_no_timeline_record_without_rows_to_order():
    assert timeline_record([]) is None


def test_the_appendix_lists_the_timeline_as_computed_without_adding_scope():
    records = with_timeline([HIT, BEFORE, AFTER])
    text = render_appendix(records)
    assert "computed in code from R1, R2, R3" in text
    assert "seconds = time from the anchor line" in text
    assert "Examples only (do not measure frequency): R1, R2, R3" in text


def test_a_finding_that_cites_the_timeline_is_not_presented_as_a_frequency():
    records = with_timeline([HIT, BEFORE, AFTER])
    answer = AnswerOutput.model_validate({
        "blocks": [{"type": "text", "text": "Story."}],
        "findings": [{
            "id": "F1", "kind": "measured",
            "statement": "Line 56 failed to spawn 1.5 seconds after line 55.",
            "result_ids": ["R4"],
        }],
    })
    text = serialize_answer(answer, {"R1", "R2", "R3", "R4"}, records)
    assert "R4 (computed)" in text
    assert "(row-level evidence, not a frequency)" in text


def test_the_timeline_renders_as_a_table_through_its_marker():
    records = with_timeline([HIT, BEFORE, AFTER])
    answer = AnswerOutput.model_validate({
        "blocks": [{"type": "text", "text": "Timeline:"},
                   {"type": "table", "result_id": "R4"}],
    })
    text = serialize_answer(answer, {r["id"] for r in records}, records)
    assert [v for kind, v in canonicalize(text) if kind == "table"] == ["R4"]


# --- answer node -------------------------------------------------------------


def _answer_state(role, records):
    return {
        "role": {"name": role, "objective": "o"},
        "effort": "Medium",
        "normalized": {"normalized_question": "q"},
        "evidence_report": {},
        "tool_results": records,
    }


def test_answer_node_adds_the_timeline_for_the_engineer_only(monkeypatch):
    prompts_seen = []

    def fake_generate(prompt):
        prompts_seen.append(prompt)
        return AnswerOutput.model_validate({"blocks": [{"type": "text", "text": "Story."}]})

    monkeypatch.setattr(graph, "generate_answer", fake_generate)
    records = [HIT, BEFORE, AFTER]
    engineer = graph.answer_node(_answer_state("engineer", records))
    team_lead = graph.answer_node(_answer_state("team_lead", records))
    assert [r["id"] for r in engineer["tool_results"]] == ["R1", "R2", "R3", "R4"]
    assert engineer["tool_results"][-1]["tool_name"] == DERIVED_TOOL_NAME
    assert DERIVED_TOOL_NAME in prompts_seen[0]
    assert all(r["tool_name"] != DERIVED_TOOL_NAME for r in team_lead["tool_results"])


def test_a_revision_does_not_duplicate_the_timeline(monkeypatch):
    monkeypatch.setattr(
        graph, "generate_answer",
        lambda prompt: AnswerOutput.model_validate({"blocks": [{"type": "text", "text": "S"}]}),
    )
    first = graph.answer_node(_answer_state("engineer", [HIT, BEFORE, AFTER]))
    second = graph.answer_node(first)
    assert [r["id"] for r in second["tool_results"]] == ["R1", "R2", "R3", "R4"]


def test_an_engineer_without_neighbouring_lines_gets_no_timeline(monkeypatch):
    monkeypatch.setattr(
        graph, "generate_answer",
        lambda prompt: AnswerOutput.model_validate({"blocks": [{"type": "text", "text": "S"}]}),
    )
    state = graph.answer_node(_answer_state("engineer", [HIT]))
    assert [r["id"] for r in state["tool_results"]] == ["R1"]
