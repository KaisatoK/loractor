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
    assert db.auto_complete("@frontend", "otel_s3") == []


# --- Template embeddings (Task 8) -------------------------------------------
def test_template_embeddings_cover_every_row_of_the_slice():
    """Completeness is an honesty requirement.

    If embeddings covered only some templates, a semantic search finding nothing
    would not be evidence that nothing is there.
    """
    from backend.db.templates import templates_status

    status = templates_status("otel_slice")
    if not status.get("exists"):
        pytest.skip("template embeddings not built")

    assert status["complete"], status
    assert status["embedded"] == status["templates"]
    assert status["rows_covered"] > 0


def test_templates_are_far_fewer_than_rows():
    """The reduction that makes embedding affordable at all.

    Measured at build time: 150,557 rows reduced to 768 templates, 196x.
    """
    from backend.db.probe import probe_cursor
    from backend.db.templates import templates_status

    status = templates_status("otel_slice")
    if not status.get("exists"):
        pytest.skip("template embeddings not built")

    with probe_cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM otel_slice")
        rows = int(cur.fetchone()["n"])

    assert status["templates"] < rows / 20


def test_semantic_search_finds_what_lexical_search_cannot():
    """The whole point of the template branch.

    'money conversion problem' shares no word with 'failed to convert shipping
    cost to currency', so BM25 alone returns nothing.
    """
    from backend.db.templates import matched_templates, templates_status

    if not templates_status("otel_slice").get("exists"):
        pytest.skip("template embeddings not built")

    matches = matched_templates("money conversion problem", "otel_slice", limit=3)
    assert matches
    assert all(m["row_count"] > 0 for m in matches)
    assert all(0 <= m["distance"] <= 2 for m in matches)


def test_matched_templates_report_how_much_corpus_they_cover():
    """A template matching 16,000 rows is a different finding from one
    matching 1, and the agent has to be able to tell them apart."""
    from backend.db.templates import matched_templates, templates_status

    if not templates_status("otel_slice").get("exists"):
        pytest.skip("template embeddings not built")

    matches = matched_templates("failed to charge card", "otel_slice", limit=5)
    assert matches
    assert all("row_count" in m and "template" in m for m in matches)


def test_fused_search_discloses_that_matching_is_template_level():
    """A template match describes a kind of event, not an individual line."""
    import backend.db as db
    from backend.db import templates

    if not templates.templates_status("otel_slice").get("exists"):
        pytest.skip("template embeddings not built")

    templates.clear_vector_cache()
    result = db.search_logs("money conversion problem", dataset="otel_slice", limit=3)
    assert len(result) > 0
    assert any("template-level" in note for note in result.stats.notes)


def test_openstack_search_still_uses_per_row_embeddings():
    """The 2,000-row corpus keeps the more precise path."""
    import backend.db as db

    result = db.search_logs("instance failed to spawn", dataset="openstack", limit=3)
    assert len(result) > 0
    assert not any("template-level" in note for note in result.stats.notes)
