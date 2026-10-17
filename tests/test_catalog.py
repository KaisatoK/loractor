"""Offline tests for the dataset catalog (``db/catalog_query.py``, T7).

Builders are reached through ``__wrapped__`` and the orchestration runs against
stubbed runners, so no database is touched. The live counterpart is the
``list_datasets`` check in ``test_openstack_live.py`` / ``test_bgl_live.py``.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest

from backend.db import catalog_query as cq
from backend.db import external
from backend.db.datasets import resolve
from backend.db.external import ExternalIndexPlan, record_build_statements
from backend.db.stats import QueryResult
from backend.pipeline.tools.envelope import TOOL_KINDS, run_enveloped
from sql_render import normalized
from tool_invoke import tool_call as _tool_call

UTC = dt.timezone.utc


def _ts(*parts) -> dt.datetime:
    return dt.datetime(*parts, tzinfo=UTC)


# --- SQL shape -------------------------------------------------------------------
@pytest.mark.parametrize("dataset", ["openstack", "bgl"])
def test_level_profile_counts_every_row_by_severity(dataset):
    query, params, stats = cq.level_profile.__wrapped__(dataset)
    text = normalized(query)

    assert 'SELECT "Level" AS severity, count(*) AS count' in text
    assert 'min("Timestamp") AS first_ts, max("Timestamp") AS last_ts' in text
    assert f'FROM "{dataset}"' in text
    assert 'GROUP BY "Level"' in text
    # Whole dataset: no window, no row cap.
    assert "WHERE" not in text
    assert "LIMIT" not in text
    assert params == []
    assert stats.dataset == dataset and stats.approximate is False


def test_existing_relations_binds_every_name():
    query, params = cq.existing_relations.__wrapped__(("bgl", "openstack"))
    assert normalized(query) == (
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_name IN (%s, %s)"
    )
    assert params == ["bgl", "openstack"]


def test_slice_metadata_reads_the_build_record():
    query, params = cq.slice_metadata.__wrapped__()
    text = normalized(query)
    assert f'FROM "{external.META_TABLE}"' in text
    assert "window_start, window_end, severities, indexed_rows" in text
    assert params == []


def test_index_row_count_reads_the_index_relation():
    query, _ = cq.index_row_count.__wrapped__("otel_slice")
    assert normalized(query) == f'SELECT count(*) AS n FROM "{external.INDEX_NAME}"'


# --- Local profile -----------------------------------------------------------------
def test_summarize_levels_is_exact_and_gives_a_window_covering_every_row():
    ds = resolve("openstack")
    rows = [
        {"severity": "WARNING", "count": 30, "first_ts": _ts(2017, 5, 16, 1), "last_ts": _ts(2017, 5, 16, 2)},
        {"severity": "INFO", "count": 970, "first_ts": _ts(2017, 5, 14, 19, 39, 1, 445000), "last_ts": _ts(2017, 5, 17, 12, 2, 35, 320000)},
    ]
    row = cq.summarize_levels(ds, rows)

    assert row["available"] is True
    assert row["rows"] == 1000 and row["rows_exact"] is True
    assert row["levels"] == [
        {"severity": "INFO", "count": 970},
        {"severity": "WARNING", "count": 30},
    ]
    assert row["first_timestamp"] == _ts(2017, 5, 14, 19, 39, 1, 445000)
    assert row["last_timestamp"] == _ts(2017, 5, 17, 12, 2, 35, 320000)
    # End is exclusive and whole-second, so it lies past the last row.
    assert row["window_start"] == "2017-05-14 19:39:01"
    assert row["window_end"] == "2017-05-17 12:02:36"
    assert row["whole_span_fits_one_window"] is True
    assert row["max_window_seconds"] == 90 * 86_400
    assert row["timestamp_source"] == "rows"


def test_a_span_wider_than_one_window_is_flagged():
    ds = resolve("openstack")
    rows = [{"severity": "INFO", "count": 1, "first_ts": _ts(2017, 1, 1), "last_ts": _ts(2017, 12, 1)}]
    assert cq.summarize_levels(ds, rows)["whole_span_fits_one_window"] is False


def test_an_empty_table_has_no_span():
    row = cq.summarize_levels(resolve("bgl"), [])
    assert row["rows"] == 0
    assert row["first_timestamp"] is None and row["window_start"] is None
    assert "no rows" in row["note"]


# --- Remote profile (footers) ----------------------------------------------------
def _footer(url, ts_column, *, groups):
    return [
        {
            "row_group_id": i,
            "row_group_num_rows": n,
            "row_group_compressed_bytes": 100,
            "stats_min_value": lo,
            "stats_max_value": hi,
        }
        for i, (n, lo, hi) in enumerate(groups)
    ]


def test_remote_profile_sums_footers_without_counting_levels(monkeypatch):
    from backend.db import pruning

    monkeypatch.setattr(pruning, "_CACHE", {})
    ds = resolve("otel_s3")
    groups = [
        (600, "2025-09-23 00:00:01.116880211+00", "2025-09-23 00:00:02.5+00"),
        (400, "2025-09-23 00:00:02.4+00", "2025-09-23 00:00:09.9+00"),
    ]
    row = cq.remote_profile(ds, fetch=lambda url, col: _footer(url, col, groups=groups))

    assert row["rows"] == 1000 * len(ds.sources)
    assert row["rows_exact"] is True
    assert row["timestamp_source"] == "parquet_footers"
    assert row["window_start"] == "2025-09-23 00:00:01+00"
    assert row["window_end"] == "2025-09-23 00:00:10+00"
    assert row["max_window_seconds"] == 300
    # No row was read, so levels are declared, never counted.
    assert {lv["severity"] for lv in row["levels"]} == set(ds.severity_values)
    assert all(lv["count"] is None for lv in row["levels"])
    assert "NOT counted" in row["note"]


# --- In-place index profile --------------------------------------------------------
def test_build_record_round_trips_the_planned_window():
    plan = ExternalIndexPlan(
        start="2025-09-23 00:00:00+00", end="2025-09-23 00:00:16+00",
        severities=("error", "fatal"), window_seconds=16, row_groups=15,
        estimated_rows=1, estimated_bytes=1, estimated_seconds=470,
    )
    statements = record_build_statements(plan, 1_401_135)
    texts = [normalized(s) for s, _ in statements]

    assert texts[0] == f'DROP TABLE IF EXISTS "{external.META_TABLE}"'
    assert texts[1].startswith(f'CREATE TABLE "{external.META_TABLE}"')
    assert texts[2].startswith(f'INSERT INTO "{external.META_TABLE}"')
    params = statements[2][1]
    assert params[:4] == ("2025-09-23 00:00:00+00", "2025-09-23 00:00:16+00", "error,fatal", 1_401_135)


def test_dropping_the_index_drops_its_build_record():
    executed = []

    class _Cur:
        def execute(self, statement, params=None):
            executed.append(normalized(statement))

    external._drop(_Cur())
    assert f'DROP TABLE IF EXISTS "{external.META_TABLE}"' in executed


def test_slice_profile_reports_the_build_window(monkeypatch):
    monkeypatch.setattr(cq, "index_row_count", lambda _ds: [{"n": 1_401_135}])
    monkeypatch.setattr(cq, "slice_metadata", lambda: [{
        "window_start": "2025-09-23 00:00:00+00", "window_end": "2025-09-23 00:00:16+00",
        "severities": "error,fatal", "indexed_rows": 1_401_135, "built_at": "x",
    }])
    row = cq.slice_profile(resolve("otel_slice"), {external.VIEW_NAME, external.META_TABLE})

    assert row["available"] is True
    assert row["rows"] == 1_401_135
    assert row["timestamp_source"] == "index_build_record"
    assert row["window_start"] == "2025-09-23 00:00:00+00"
    # A build window is already end-exclusive: used as is, not padded.
    assert row["window_end"] == "2025-09-23 00:00:16+00"
    assert row["span_seconds"] == 16
    assert [lv["severity"] for lv in row["levels"]] == ["error", "fatal"]
    assert "BUILT over" in row["note"]


def test_slice_profile_falls_back_to_the_view_definition(monkeypatch):
    definition = (
        'SELECT "Timestamp", "SeverityText" FROM read_parquet(\'https://x/p.parquet\') '
        "WHERE \"Timestamp\" >= '2025-09-23 00:00:00+00'::timestamptz "
        "AND \"Timestamp\" < '2025-09-23 00:00:16+00'::timestamptz "
        "AND \"SeverityText\" IN ('error', 'fatal')"
    )
    monkeypatch.setattr(cq, "index_row_count", lambda _ds: [{"n": 7}])
    monkeypatch.setattr(cq, "slice_view_definition", lambda: [{"view_definition": definition}])
    row = cq.slice_profile(resolve("otel_slice"), {external.VIEW_NAME})

    assert row["timestamp_source"] == "index_view_definition"
    assert row["window_start"] == "2025-09-23 00:00:00+00"
    assert row["window_end"] == "2025-09-23 00:00:16+00"
    assert [lv["severity"] for lv in row["levels"]] == ["error", "fatal"]


def test_an_unbuilt_index_is_unavailable_not_an_error():
    row = cq.slice_profile(resolve("otel_slice"), set())
    assert row["available"] is False
    assert "not been built" in row["note"]


def test_parse_view_window_rejects_an_illegible_definition():
    assert cq.parse_view_window("SELECT 1") is None


# --- The catalog -----------------------------------------------------------------
def _stub_catalog(monkeypatch, *, present, local=None):
    monkeypatch.setattr(
        cq, "existing_relations", lambda names: [{"table_name": n} for n in names if n in present]
    )

    def level_profile(dataset_id):
        if local and dataset_id in local:
            raise local[dataset_id]
        return QueryResult([
            {"severity": "INFO", "count": 10, "first_ts": _ts(2017, 5, 14), "last_ts": _ts(2017, 5, 15)},
        ])

    monkeypatch.setattr(cq, "level_profile", level_profile)
    monkeypatch.setattr(
        cq, "remote_profile", lambda ds, fetch=None: cq.unavailable(ds, "stub")
    )


def test_list_datasets_has_one_row_per_registered_dataset(monkeypatch):
    from backend.db.datasets import dataset_ids

    _stub_catalog(monkeypatch, present={"openstack", "bgl"})
    rows = cq.list_datasets()

    assert [r["dataset"] for r in rows] == list(dataset_ids())
    by_id = {r["dataset"]: r for r in rows}
    assert by_id["openstack"]["rows"] == 10
    assert by_id["otel_slice"]["available"] is False
    assert rows.stats.est_rows_scanned == 20


def test_an_unloaded_table_is_reported_not_raised(monkeypatch):
    _stub_catalog(monkeypatch, present={"openstack"})
    by_id = {r["dataset"]: r for r in cq.list_datasets()}

    assert by_id["bgl"]["available"] is False
    assert "setup.sh" in by_id["bgl"]["note"]
    assert by_id["openstack"]["available"] is True


def test_one_failing_dataset_does_not_hide_the_others(monkeypatch):
    _stub_catalog(monkeypatch, present={"openstack", "bgl"}, local={"bgl": RuntimeError("boom")})
    by_id = {r["dataset"]: r for r in cq.list_datasets()}

    assert by_id["bgl"]["available"] is False
    assert "RuntimeError" in by_id["bgl"]["note"]
    assert by_id["openstack"]["rows"] == 10


def test_the_catalog_fails_when_no_dataset_can_be_profiled(monkeypatch):
    def down(*_a, **_k):
        raise ConnectionError("db down")

    monkeypatch.setattr(cq, "existing_relations", lambda names: [{"table_name": n} for n in names])
    monkeypatch.setattr(cq, "level_profile", down)
    monkeypatch.setattr(cq, "remote_profile", down)
    monkeypatch.setattr(cq, "slice_profile", down)
    with pytest.raises(RuntimeError, match="no dataset"):
        cq.list_datasets()


def test_the_envelope_is_a_complete_json_safe_catalog(monkeypatch):
    _stub_catalog(monkeypatch, present={"openstack", "bgl"})
    envelope = run_enveloped(
        operation="list datasets", function=cq.list_datasets, kind="catalog", complete=True
    )

    assert envelope["status"] == "success"
    assert envelope["kind"] == "catalog" == TOOL_KINDS["list_datasets_tool"]
    assert envelope["truncated"] is False
    assert envelope["returned"] == len(envelope["data"])
    json.dumps(envelope)  # raises on a datetime or Decimal left in the rows
    first = next(r for r in envelope["data"] if r["dataset"] == "openstack")
    assert first["first_timestamp"] == "2017-05-14 00:00:00+00:00"


# --- The tool on both transports --------------------------------------------------
def test_the_local_tool_takes_no_arguments_and_returns_the_catalog(monkeypatch):
    """Invoked as the agent's ToolNode does: no model arguments, runtime
    injected. Calling ``.func`` directly hid a bug where an empty args_schema
    made langchain-core drop the runtime, so every real call failed."""
    from backend.pipeline.tools import catalog_tools

    _stub_catalog(monkeypatch, present={"openstack", "bgl"})
    monkeypatch.setattr(catalog_tools, "list_datasets", cq.list_datasets)

    events: list = []
    message = catalog_tools.list_datasets_tool.invoke(
        _tool_call(catalog_tools.list_datasets_tool, events)
    )
    result = json.loads(message.content)

    assert result["kind"] == "catalog"
    assert {r["dataset"] for r in result["data"]} >= {"openstack", "bgl"}
    assert [e["status"] for e in events] == ["started", "completed"]


@pytest.mark.parametrize("transport", ["local", "mcp"])
def test_the_model_sees_no_arguments_and_runtime_stays_injected(transport):
    from backend.pipeline.tools import catalog_tools, mcp_tools

    tool = (catalog_tools if transport == "local" else mcp_tools).list_datasets_tool
    assert list(tool.tool_call_schema.model_fields) == []
    assert "runtime" in tool._injected_args_keys


def test_the_mcp_server_offers_the_catalog_with_no_inputs():
    import asyncio

    from fastmcp import Client

    from backend.mcp_server import server

    async def run():
        async with Client(server.mcp) as client:
            tools = {t.name: t for t in await client.list_tools()}
            tool = tools["list_datasets_tool"]
            assert "window_start" in tool.description
            assert not tool.input_schema.get("required")

    asyncio.run(run())
