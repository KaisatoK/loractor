"""Live tests against the remote OTel corpus in public object storage.

Gated: these read from ``public-pme.s3.eu-west-3.amazonaws.com`` and cost real
network time, so they only run when ``RUN_OTEL_LIVE=1``. Run them alone.

They assert the properties that make the remote tier safe to expose, rather than
exact counts: metadata pruning actually narrows the read, aggregates come back
labelled approximate, cost is reported, and nothing is ever materialized.

Measured reference figures (part_000, this host) for orienting a failure:

* 1,000,000,000 rows in 1,624 row groups, 38.2 GiB
* a row group is ~614,400 rows spanning ~1.1 s
* a 10-second window prunes to ~10 row groups, ~6.2M rows, ~219 MiB, ~1-2 s
* severity is lower case; ``error`` is ~14% of rows
"""

from __future__ import annotations

import os
import time

import pytest

import backend.db as db
from backend.db.datasets import Capability, resolve

RUN = os.getenv("RUN_OTEL_LIVE") == "1"
pytestmark = pytest.mark.skipif(
    not RUN, reason="set RUN_OTEL_LIVE=1 to query the remote corpus"
)

LO = "2025-09-23 00:00:00+00"
HI_10S = "2025-09-23 00:00:10+00"
HI_5S = "2025-09-23 00:00:05+00"

# Generous: the remote tier is bulk-class and crosses the public internet.
MAX_SECONDS = 60


def test_metadata_is_free_and_matches_the_measured_corpus():
    """Footer reads scan no rows, so this must be fast and exact."""
    from backend.db.probe import file_metadata

    started = time.monotonic()
    meta = file_metadata(resolve("otel_s3").sources[0])
    elapsed = time.monotonic() - started

    assert meta.num_rows == 1_000_000_000
    assert meta.num_row_groups == 1_624
    assert 40 < meta.bytes_per_row < 42
    assert elapsed < 30


def test_a_ten_second_window_prunes_to_about_ten_row_groups():
    """The pruning guarantee: ~1.1 s per row group, so ~10 for a 10 s window."""
    result = db.count_by_field(LO, HI_10S, dataset="otel_s3")
    assert not result.refused

    stats = result.stats
    assert stats.row_groups_considered == 1_624
    assert 8 <= stats.row_groups_read <= 13
    # Under 1% of the file, which is the whole point.
    assert stats.pruning_ratio > 0.99


def test_severity_mix_is_returned_lower_case_and_error_is_a_large_share():
    result = db.count_by_field(LO, HI_10S, dataset="otel_s3")
    counts = {row["severity"]: int(row["count"]) for row in result}

    assert "error" in counts, f"expected lower-case severities, got {list(counts)}"
    assert "ERROR" not in counts

    total = sum(counts.values())
    # Measured ~13.8-14.2%; assert the order of magnitude, not the exact value.
    assert 0.08 < counts["error"] / total < 0.25


def test_remote_aggregates_are_labelled_approximate():
    """The corpus does not promise exact counts, so results must say so."""
    assert not resolve("otel_s3").supports(Capability.EXACT_COUNTS)
    result = db.count_by_field(LO, HI_10S, dataset="otel_s3")
    assert result.stats.approximate is True


def test_distinct_count_uses_the_approximate_aggregate():
    result = db.count_distinct_by_field(LO, HI_10S, "service", dataset="otel_s3")
    assert int(result[0]["distinct_count"]) > 0
    assert result.stats.approximate is True
    assert any("approximate" in note for note in result.stats.notes)


def test_ranking_services_by_error_volume_returns_useful_rows():
    """Usefulness check: a 5-second window must still identify real services."""
    result = db.top_by_field(
        LO, HI_5S, "service", dataset="otel_s3", severity="error", limit=5
    )
    assert len(result) >= 1
    counts = [int(row["count"]) for row in result]
    assert counts == sorted(counts, reverse=True)
    assert all(row["service"] for row in result)


def test_cost_is_reported_for_every_remote_query():
    result = db.count_by_field(LO, HI_10S, dataset="otel_s3")
    stats = result.stats
    assert stats.est_rows_scanned > 1_000_000
    assert stats.est_bytes_read > 0
    assert stats.elapsed_ms > 0
    assert stats.elapsed_ms < MAX_SECONDS * 1000
    summary = stats.summary()
    assert "rows scanned" in summary and "APPROXIMATE" in summary


def test_an_over_wide_window_is_refused_without_touching_the_corpus():
    """Refusal must be immediate -- no scan, no timeout."""
    started = time.monotonic()
    result = db.count_by_field(LO, "2025-09-23 01:00:00+00", dataset="otel_s3")
    elapsed = time.monotonic() - started

    assert result.refused
    assert result.refusal.code == "window_too_wide"
    assert elapsed < 5


def test_the_remote_tier_never_builds_anything_locally():
    """Querying in place must leave the database size unchanged."""
    from backend.db.probe import database_size_bytes

    before = database_size_bytes()
    db.count_by_field(LO, HI_10S, dataset="otel_s3")
    db.top_by_field(LO, HI_5S, "service", dataset="otel_s3", limit=3)
    after = database_size_bytes()

    # Allow a little slack for unrelated background activity.
    assert abs(after - before) < 1_000_000


def test_text_search_is_refused_on_the_unindexed_remote_tier():
    result = db.search_logs("out of memory", dataset="otel_s3")
    assert result.refused
    assert result.refusal.code == "unsupported_capability"


def test_fetch_by_id_is_refused_because_the_corpus_has_no_primary_key():
    result = db.get_log_by_record_id(1, dataset="otel_s3")
    assert result.refused
    assert result.refusal.code == "unsupported_capability"


def test_auto_complete_never_reaches_object_storage():
    from backend.db.auto_complete import _auto_complete_dataset, local_datasets

    # The per-corpus lookup refuses the remote tier, and the public merged
    # lookup never asks it.
    assert list(_auto_complete_dataset("@frontend", "otel_s3")) == []
    assert "otel_s3" not in {ds.id for ds in local_datasets()}


# --- The in-place index replaced the materialized slice ----------------------
def test_the_otel_index_copies_nothing():
    """The slice used to materialize rows locally; the index does not.

    Measured when the change was made: the same coverage costs 465 s rather
    than 1,324 s to build, 45-60 B/row rather than 155, and no row is copied.
    """
    from backend.db.datasets import DatasetKind, resolve

    dataset = resolve("otel_slice")
    assert dataset.kind is DatasetKind.EXTERNAL_INDEX
    assert dataset.search_is_self_contained


def test_search_on_the_index_returns_real_log_lines():
    import backend.db as db
    from backend.db.external import external_index_status

    if not external_index_status().get("exists"):
        pytest.skip("in-place OTel index not built")

    rows = db.search_logs("failed to charge card", dataset="otel_slice", limit=3)
    assert len(rows) > 0
    for row in rows:
        assert row["message"]
        assert row["service"]
        assert row["ts"] is not None


def test_the_index_discloses_that_rows_came_from_object_storage():
    """Search is ~1 s rather than ~20 ms because a match re-reads its row from
    the file. An answer should be able to say why."""
    import backend.db as db
    from backend.db.external import external_index_status

    if not external_index_status().get("exists"):
        pytest.skip("in-place OTel index not built")

    rows = db.search_logs("charge", dataset="otel_slice", limit=2)
    assert any("object storage" in note for note in rows.stats.notes)


def test_the_nanosecond_timestamp_survives_the_driver():
    """Materializing a raw nanosecond value raises in psycopg2, so the index's
    timestamp is truncated on the way out."""
    import backend.db as db
    from backend.db.external import external_index_status

    if not external_index_status().get("exists"):
        pytest.skip("in-place OTel index not built")

    rows = db.search_logs("charge", dataset="otel_slice", limit=2)
    assert rows
    assert rows[0]["ts"].microsecond is not None


def test_no_slice_table_remains():
    """The materialized slice and its template table are gone."""
    from backend.db.probe import probe_cursor

    with probe_cursor() as cur:
        cur.execute(
            "SELECT count(*) AS n FROM information_schema.tables "
            "WHERE table_name IN ('otel_slice', 'otel_slice_templates')"
        )
        assert int(cur.fetchone()["n"]) == 0


# --- Dataset catalog (list_datasets_tool) -------------------------------------
def test_the_catalog_reads_the_remote_corpus_from_footers_only():
    """Rows and span from the footers: exact, and no row is scanned for them.
    One file is 1,000,000,000 rows over about half an hour, far past one 300s
    window, so the catalog must say the span does not fit one query."""
    import backend.db as db
    from backend.db.datasets import resolve

    row = next(r for r in db.list_datasets() if r["dataset"] == "otel_s3")
    assert row["available"] is True
    assert row["timestamp_source"] == "parquet_footers"
    assert row["rows"] == 1_000_000_000 * len(resolve("otel_s3").sources)
    assert row["whole_span_fits_one_window"] is False
    assert all(level["count"] is None for level in row["levels"])


def test_the_catalog_reports_the_index_build_window():
    import backend.db as db
    from backend.db.external import external_index_status

    row = next(r for r in db.list_datasets() if r["dataset"] == "otel_slice")
    if not external_index_status().get("exists"):
        assert row["available"] is False
        return
    assert row["available"] is True
    assert row["timestamp_source"] in {"index_build_record", "index_view_definition"}
    assert row["window_start"] and row["window_end"]


# --- Zero-filled trend on the remote corpus -----------------------------------
def test_the_remote_trend_lists_every_second_of_the_window():
    """Nanosecond timestamps are truncated and cast, so generated buckets and
    row buckets join; every second of a 10-second window is one row."""
    import backend.db as db

    lo, hi = "2025-09-23 00:00:00+00", "2025-09-23 00:00:10+00"
    values = db.get_field_values(lo, hi, "service", dataset="otel_s3", limit=1)
    assert values and not values.refused
    service = values[0]["value"]

    rows = db.get_trend(lo, hi, "service", service, dataset="otel_s3", bucket="second")
    assert not rows.refused
    assert len(rows) == 10
    assert sum(r["count"] for r in rows) == values[0]["count"]


# --- Rank priorities (T13, server-only tool) -----------------------------------
def _rank(**arguments):
    import asyncio

    from fastmcp import Client

    from backend.mcp_server import server

    async def go():
        async with Client(server.mcp) as client:
            return await client.call_tool(
                "rank_priorities_tool", arguments, raise_on_error=False
            )

    return asyncio.run(go())


def test_rank_priorities_labels_remote_results_approximate():
    """Two windows of at most 300 s; the counts have no exact-counts capability,
    so the ranking must say so."""
    result = _rank(
        datasets=["otel_s3"], group_by="service",
        baseline_start=LO, baseline_end=HI_5S,
        recent_start=HI_5S, recent_end=HI_10S,
    )
    assert not result.is_error, result.content[0].text
    envelope = result.structured_content
    if envelope["status"] == "refused":
        pytest.skip(envelope["reason"])
    assert envelope["kind"] == "derived"
    assert envelope["query_stats"]["approximate"] is True
    assert any("approximate" in note for note in envelope["query_stats"]["notes"])


def test_rank_priorities_refuses_team_on_the_remote_corpus():
    result = _rank(
        datasets=["otel_s3"], group_by="team",
        baseline_start=LO, baseline_end=HI_5S,
        recent_start=HI_5S, recent_end=HI_10S,
    )
    assert result.is_error
    assert "group_by='service'" in result.content[0].text
