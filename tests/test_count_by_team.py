"""Offline tests for T3 team ownership counts."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from backend.db import catalog_query, count_query
from backend.db.datasets import local_datasets
from backend.db.errors import QueryRefused
from backend.db.stats import QueryResult
from backend.pipeline.data_scope import build_data_scope
from backend.pipeline.tools.envelope import MEASURED_KINDS, TOOL_KINDS
from backend.pipeline.tools.schemas import CountByTeam
from sql_render import normalized


@pytest.fixture
def loaded(monkeypatch):
    monkeypatch.setattr(
        catalog_query,
        "existing_relations",
        lambda names: QueryResult([
            {"table_name": name} for name in names
        ]),
    )


def _build(*args, **kwargs):
    return count_query.count_by_team.__wrapped__(*args, **kwargs)


def test_local_datasets_are_the_only_exact_team_count_sources():
    assert {dataset.id for dataset in local_datasets()} == {"bgl", "openstack"}


def test_one_union_statement_joins_ownership_and_preserves_unowned(loaded):
    query, params, stats = _build()
    text = normalized(query)

    assert text.count("UNION ALL") == 1
    assert 'LEFT JOIN "team_ownership" AS o' in text
    assert 'o.dataset = \'BGL\'' in text
    assert 'o.dataset = \'OpenStack\'' in text
    assert 'o.component = t."Component"' in text
    assert "COALESCE(o.team, 'unowned') AS team" in text
    assert 't."Level" AS severity' in text
    assert "ORDER BY count DESC, dataset, team, severity" in text
    assert "WHERE" not in text and "LIMIT" not in text
    assert params == []
    assert stats.approximate is False
    assert stats.dataset == "bgl,openstack"
    assert any("every row" in note for note in stats.notes)


def test_window_and_category_are_applied_in_each_dataset_branch(loaded):
    query, params, stats = _build(
        ("openstack", "bgl", "openstack"),
        "2017-05-16 00:00:00",
        "2017-05-17 00:00:00",
        "ERROR",
    )
    text = normalized(query)

    assert text.count("UNION ALL") == 1
    assert text.count('"t"."Timestamp" >= %s::timestamptz') == 2
    assert text.count('"Level" = %s') == 2
    assert params == [
        "2017-05-16 00:00:00", "2017-05-17 00:00:00", "ERROR",
        "2017-05-16 00:00:00", "2017-05-17 00:00:00", "ERROR",
    ]
    assert stats.dataset == "openstack,bgl"
    assert any("half-open" in note for note in stats.notes)


def test_remote_dataset_is_refused_with_windowed_alternative(loaded):
    with pytest.raises(QueryRefused) as refused:
        _build(("otel_s3",))
    assert refused.value.refusal.code == "not_countable_by_team"
    assert "count_by_category_tool" in refused.value.refusal.suggestion


def test_missing_log_table_is_refused_not_dropped(monkeypatch):
    monkeypatch.setattr(
        catalog_query,
        "existing_relations",
        lambda names: QueryResult([{"table_name": "team_ownership"}]),
    )
    with pytest.raises(QueryRefused) as refused:
        _build(("openstack",))
    assert refused.value.refusal.code == "not_loaded"
    assert "openstack" in refused.value.refusal.reason


def test_missing_ownership_table_is_refused(monkeypatch):
    monkeypatch.setattr(
        catalog_query,
        "existing_relations",
        lambda names: QueryResult([{"table_name": "openstack"}]),
    )
    with pytest.raises(QueryRefused) as refused:
        _build(("openstack",))
    assert refused.value.refusal.code == "ownership_not_loaded"
    assert "team_ownership" in refused.value.refusal.reason


def test_category_absent_from_one_dataset_is_skipped(loaded):
    query, _, stats = _build(("openstack", "bgl"), category="FATAL")
    text = normalized(query)
    assert "'bgl'" in text and "'openstack'" not in text
    assert any("openstack" in note for note in stats.notes)


def test_wrong_case_category_is_refused(loaded):
    with pytest.raises(QueryRefused) as refused:
        _build(("openstack",), category="error")
    assert refused.value.refusal.code == "severity_case"


def test_schema_requires_paired_window_and_local_ids():
    assert CountByTeam().datasets is None
    assert CountByTeam().start_time is None
    CountByTeam(start_time="2017-05-16 00:00:00", end_time="2017-05-17 00:00:00")
    assert CountByTeam(datasets=["bgl", "bgl"]).datasets == ["bgl"]
    with pytest.raises(ValidationError):
        CountByTeam(datasets=["otel_s3"])
    with pytest.raises(ValidationError):
        CountByTeam(start_time="2017-05-16 00:00:00")
    with pytest.raises(ValidationError):
        CountByTeam(
            start_time="2017-05-17 00:00:00",
            end_time="2017-05-16 00:00:00",
        )


def test_data_scope_names_team_count_datasets_and_window():
    scope = build_data_scope([{
        "id": "R1",
        "kind": "aggregate",
        "dataset": None,
        "filters": {
            "datasets": ["bgl", "openstack"],
            "start_time": "2017-05-16 00:00:00",
            "end_time": "2017-05-17 00:00:00",
            "category": "ERROR",
        },
        "truncated": False,
    }])
    assert scope.datasets == ["bgl", "openstack"]
    assert scope.time_windows == [{
        "start": "2017-05-16 00:00:00",
        "end": "2017-05-17 00:00:00",
    }]
    assert scope.filters == {"category": ["ERROR"]}
    assert scope.measured_result_ids == ["R1"]


def test_team_tool_contract_is_measured_and_json_safe(monkeypatch):
    from backend.pipeline.tools import count_by_team_tool
    from tool_invoke import tool_call

    rows = QueryResult([
        {"dataset": "openstack", "team": "API Gateway", "severity": "ERROR", "count": 12},
        {"dataset": "bgl", "team": "unowned", "severity": "FATAL", "count": 3},
    ])
    monkeypatch.setattr(
        "backend.pipeline.tools.specs.count_by_team",
        lambda **kwargs: rows,
    )
    events: list = []
    message = count_by_team_tool.invoke(tool_call(
        count_by_team_tool,
        events,
        datasets=["openstack", "bgl"],
    ))
    result = json.loads(message.content)
    assert [event["status"] for event in events] == ["started", "completed"]
    assert result["kind"] == TOOL_KINDS["count_by_team_tool"] == "aggregate"
    assert result["kind"] in MEASURED_KINDS
    assert result["truncated"] is False
    assert result["returned"] == 2
    json.dumps(result)
