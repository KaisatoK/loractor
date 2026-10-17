"""Offline tests for code-built evidence."""

import json
import os

os.environ.setdefault("TYPESAFE_API_KEY", "test-key")

from langchain_core.messages import AIMessage, ToolMessage

from backend.pipeline.evidence import build_evidence, evidence_lines
from backend.pipeline.prompts.builders import summarized_attempts
from backend.pipeline.tools.results import collect_tool_calls, collect_tool_results


def _ai(*calls):
    return AIMessage(
        content="",
        tool_calls=[{"id": i, "name": n, "args": a} for i, n, a in calls],
    )


def _ok(call_id, data):
    return ToolMessage(
        content=json.dumps({"status": "success", "kind": "search", "data": data}),
        tool_call_id=call_id,
    )


def _transcript():
    return [
        _ai(
            ("c1", "search_logs_tool", {"query_text": "x"}),
            ("c2", "get_trend_tool", {"type": "service"}),
            ("c3", "count_by_category_tool", {"type": "pid"}),
            ("c4", "read_file", {"path": "skills/x"}),
        ),
        _ok("c1", [{"a": 1}, {"a": 2}]),
        _ok("c2", []),
        ToolMessage(
            content="The tool could not complete the database operation: db down",
            tool_call_id="c3",
            status="error",
        ),
        ToolMessage(content="skill text", tool_call_id="c4"),
    ]


def test_calls_cover_success_empty_and_failed_and_ignore_non_db_tools():
    messages = _transcript()
    records = collect_tool_results(messages)
    calls = collect_tool_calls(messages, records)

    assert [c["tool_name"] for c in calls] == [
        "search_logs_tool",
        "get_trend_tool",
        "count_by_category_tool",
    ]
    assert [c["status"] for c in calls] == ["success", "success", "failure"]
    assert "db down" in calls[2]["error_message"]
    assert calls[0]["error_message"] is None
    assert calls[0]["arguments"] == {"query_text": "x"}


def test_result_ids_are_linked_by_call_id():
    messages = [
        _ai(("c1", "search_logs_tool", {}), ("c2", "get_data_tool", {})),
        _ok("c2", [{"b": 1}]),  # answered out of order
        _ok("c1", [{"a": 1}]),
    ]
    records = collect_tool_results(messages)
    assert [(r["id"], r["tool_call_id"]) for r in records] == [("R1", "c2"), ("R2", "c1")]
    calls = collect_tool_calls(messages, records)
    assert [(c["call_id"], c["result_ids"]) for c in calls] == [("c1", ["R2"]), ("c2", ["R1"])]


def test_envelope_failure_and_missing_reply_are_failures():
    messages = [
        _ai(("c1", "search_logs_tool", {}), ("c2", "get_data_tool", {}), ("c3", "get_data_tool", {})),
        ToolMessage(content=json.dumps({"status": "failure", "error": "bad"}), tool_call_id="c1"),
        ToolMessage(content="not json", tool_call_id="c2"),
    ]
    calls = collect_tool_calls(messages, [])
    assert [c["status"] for c in calls] == ["failure"] * 3
    assert calls[0]["error_message"] == "bad"
    assert "not valid JSON" in calls[1]["error_message"]
    assert "no tool result" in calls[2]["error_message"]


def test_build_evidence_one_item_per_call_with_availability():
    messages = _transcript()
    records = collect_tool_results(messages)
    evidence = build_evidence(collect_tool_calls(messages, records), records)

    assert [e.id for e in evidence] == ["E1", "E2", "E3"]
    assert [e.name for e in evidence] == [
        "search_logs_tool",
        "get_trend_tool",
        "count_by_category_tool",
    ]
    assert [e.availability for e in evidence] == ["available", "unavailable", "unavailable"]
    assert [e.result_ids for e in evidence] == [["R1"], [], []]
    assert evidence[0].description == "search query: {'query_text': 'x'}"
    failed = evidence[2].tool_used[0]
    assert failed.status == "failure" and "db down" in failed.error_message
    assert evidence[1].tool_used[0].status == "success"  # empty is not an error


def test_evidence_lines_format():
    messages = _transcript()
    records = collect_tool_results(messages)
    evidence = build_evidence(collect_tool_calls(messages, records), records)
    lines = evidence_lines(evidence, records).splitlines()
    assert lines[0] == "- E1: search_logs_tool({'query_text': 'x'}) -> R1 [search, 2 rows]"
    assert lines[1] == "- E2: get_trend_tool({'type': 'service'}) -> no rows"
    assert lines[2].startswith("- E3: count_by_category_tool({'type': 'pid'}) -> failed: ")


def test_attempted_tools_include_failed_and_empty_calls():
    messages = _transcript()
    records = collect_tool_results(messages)
    calls = collect_tool_calls(messages, records)
    attempts = summarized_attempts(records, calls)
    assert [a["status"] for a in attempts] == ["success", "success", "failure"]
    assert [a["row_count"] for a in attempts] == [2, 0, 0]
    assert "db down" in attempts[2]["error_message"]
    assert "error_message" not in attempts[0]


def test_attempted_tools_fall_back_to_records_without_calls():
    records = [{"id": "R1", "tool_name": "t", "arguments": {}, "row_count": 3}]
    assert summarized_attempts(records) == [
        {"tool_name": "t", "arguments": {}, "row_count": 3}
    ]
