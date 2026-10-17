"""Offline tests for whole-dataset severity counts (``count_by_dataset_tool``, T6).

The builder is reached through ``__wrapped__`` with the existence check stubbed,
so no database is touched. The live check is in ``test_bgl_live.py`` (it needs
both local corpora loaded).
"""

from __future__ import annotations

import asyncio
import json

import pytest
from pydantic import ValidationError

from backend.db import catalog_query, count_query
from backend.db.datasets import local_datasets
from backend.db.errors import QueryRefused
from backend.db.stats import QueryResult
from backend.pipeline.data_scope import build_data_scope
from backend.pipeline.tools.envelope import MEASURED_KINDS, TOOL_KINDS
from backend.pipeline.tools.schemas import CountByDataset
from sql_render import normalized


@pytest.fixture
def loaded(monkeypatch):
    """Every requested local table exists."""
    monkeypatch.setattr(
        catalog_query,
        "existing_relations",
        lambda names: QueryResult([{"table_name": n} for n in names]),
    )


def _build(datasets=None):
    return count_query.count_by_dataset.__wrapped__(datasets)


def test_only_local_exact_datasets_are_countable_whole():
    assert {d.id for d in local_datasets()} == {"bgl", "openstack"}


def test_one_statement_counts_every_local_dataset_by_severity(loaded):
    query, params, stats = _build()
    text = normalized(query)

    assert text.count("UNION ALL") == 1
    assert "SELECT 'bgl' AS dataset, \"Level\" AS severity, count(*) AS count FROM \"bgl\" GROUP BY \"Level\"" in text
    assert "SELECT 'openstack' AS dataset, \"Level\" AS severity, count(*) AS count FROM \"openstack\" GROUP BY \"Level\"" in text
    assert "ORDER BY dataset, count DESC" in text
    # Whole datasets: no window and no row cap.
    assert "WHERE" not in text and "LIMIT" not in text
    assert params == []
    assert stats.approximate is False
    assert stats.dataset == "bgl,openstack"


def test_a_subset_counts_only_the_named_dataset(loaded):
    text = normalized(_build(("openstack", "openstack"))[0])
    assert "UNION ALL" not in text
    assert 'FROM "openstack"' in text and '"bgl"' not in text


def test_the_remote_corpus_is_refused_with_an_alternative(loaded):
    with pytest.raises(QueryRefused) as refused:
        _build(("otel_s3",))
    assert refused.value.refusal.code == "not_countable_whole"
    assert "count_by_category_tool" in refused.value.refusal.suggestion


def test_an_unloaded_dataset_is_refused_not_dropped(monkeypatch):
    monkeypatch.setattr(
        catalog_query,
        "existing_relations",
        lambda names: QueryResult([{"table_name": "openstack"}]),
    )
    with pytest.raises(QueryRefused) as refused:
        _build()
    assert refused.value.refusal.code == "not_loaded"
    assert "bgl" in refused.value.refusal.reason


def test_the_schema_accepts_local_ids_only():
    assert CountByDataset().datasets is None
    assert CountByDataset(datasets=["bgl", "bgl"]).datasets == ["bgl"]
    for bad in (["otel_s3"], ["otel_slice"], ["nope"], []):
        with pytest.raises(ValidationError):
            CountByDataset(datasets=bad)


def test_the_result_is_a_measured_complete_aggregate(monkeypatch):
    from backend.pipeline.tools import count_tools

    rows = QueryResult([
        {"dataset": "bgl", "severity": "FATAL", "count": 855_501},
        {"dataset": "openstack", "severity": "ERROR", "count": 194},
    ])
    monkeypatch.setattr(count_tools, "count_by_dataset", lambda datasets: rows)

    class _Runtime:
        def __init__(self):
            self.events = []
            self.stream_writer = self.events.append

    result = count_tools.count_by_dataset_tool.func(runtime=_Runtime(), datasets=None)
    assert result["kind"] == "aggregate" == TOOL_KINDS["count_by_dataset_tool"]
    assert result["kind"] in MEASURED_KINDS
    assert result["truncated"] is False
    assert result["returned"] == 2
    json.dumps(result)


def test_the_data_scope_names_every_counted_dataset():
    scope = build_data_scope([{
        "id": "R1", "kind": "aggregate", "dataset": None,
        "filters": {"datasets": ["bgl", "openstack"]}, "truncated": False,
    }])
    assert scope.datasets == ["bgl", "openstack"]
    assert "datasets" not in scope.filters
    assert scope.measured_result_ids == ["R1"]


def test_the_mcp_server_rejects_the_remote_corpus():
    from backend.mcp_server import server

    async def run():
        from fastmcp import Client

        async with Client(server.mcp) as client:
            tools = {t.name for t in await client.list_tools()}
            assert "count_by_dataset_tool" in tools
            result = await client.call_tool(
                "count_by_dataset_tool", {"datasets": ["otel_s3"]}, raise_on_error=False
            )
            assert result.is_error  # rejected by validation, before any query

    asyncio.run(run())
