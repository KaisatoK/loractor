"""Offline tests for the code-built data scope and the extended hypothesis contract."""

import os

os.environ.setdefault("TYPESAFE_API_KEY", "test-key")

from backend.pipeline import graph
from backend.pipeline.contracts import EvidenceReport, Hypothesis
from backend.pipeline.data_scope import build_data_scope


def _rec(rid, kind, filters=None, truncated=False, dataset="OpenStack", arguments=None):
    return {
        "id": rid,
        "kind": kind,
        "dataset": dataset,
        "filters": filters,
        "truncated": truncated,
        "arguments": arguments or {},
    }


def test_scope_separates_measured_from_examples_and_flags_truncation():
    records = [
        _rec("R1", "search", {"query_text": "timeout", "category": "ERROR"}, truncated=True),
        _rec(
            "R2",
            "aggregate",
            {"start_time": "2017-05-16 00:00:00", "end_time": "2017-05-16 00:15:00",
             "category": "ERROR", "group_by": "service, category"},
        ),
        _rec("R3", "trend", {"start_time": "2017-05-16 00:00:00",
                             "end_time": "2017-05-16 00:15:00"}, dataset="BGL",
             truncated=None),
    ]
    scope = build_data_scope(records)
    assert scope.datasets == ["BGL", "OpenStack"]
    assert scope.measured_result_ids == ["R2", "R3"]
    assert scope.example_result_ids == ["R1"]
    assert scope.truncated_result_ids == ["R1"]
    assert scope.unknown_truncation_result_ids == ["R3"]
    assert scope.time_windows == [
        {"start": "2017-05-16 00:00:00", "end": "2017-05-16 00:15:00"}
    ]
    assert scope.filters["category"] == ["ERROR"]
    assert scope.filters["query_text"] == ["timeout"]


def test_scope_falls_back_to_arguments_for_legacy_records():
    legacy = {"id": "R1", "kind": "aggregate", "dataset": None, "filters": None,
              "truncated": None,
              "arguments": {"baseline_start": "a", "baseline_end": "b", "limit": 5}}
    scope = build_data_scope([legacy])
    assert scope.time_windows == [{"start": "a", "end": "b", "label": "baseline"}]
    assert scope.datasets == []


def test_hypothesis_contradictions_and_next_check_default_empty():
    h = Hypothesis(hypothesis="h", description="d", status="inconclusive", evidence_ids=[])
    assert h.contradicting_evidence_ids == [] and h.next_check is None


def test_find_evidence_node_attaches_code_built_scope(monkeypatch):
    class _Agent:
        def invoke(self, payload):
            return {"messages": []}

    monkeypatch.setattr(graph, "get_find_evidence_agent", lambda effort: _Agent())
    monkeypatch.setattr(
        graph, "collect_tool_results",
        lambda messages: [_rec("R1", "aggregate", {"category": "ERROR"})],
    )
    monkeypatch.setattr(
        graph, "generate_evidence_report",
        lambda **kwargs: EvidenceReport(summary="s", evidence=[]),
    )
    state = {
        "effort": "Low",
        "role": {"name": "engineer", "objective": "o"},
        "normalized": {"normalized_question": "q"},
    }
    out = graph.find_evidence_node(state)
    assert out["evidence_report"]["data_scope"]["measured_result_ids"] == ["R1"]
