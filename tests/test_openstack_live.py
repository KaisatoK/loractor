"""Live OpenStack checks against a loaded corpus.

Gated by ``RUN_OPENSTACK_LIVE=1`` because they need the corpus loaded:

    bash ./setup.sh     # or: uv run python -m backend.db.loghub openstack --no-embed
    RUN_OPENSTACK_LIVE=1 uv run pytest tests/test_openstack_live.py

Kept out of the default suite so a fresh checkout does not fail for want of a
corpus nobody loaded. The offline proof that the registry and the loader handle
OpenStack lives in ``test_openstack.py``; this file confirms the queries
actually return data.
"""

from __future__ import annotations

import os

import pytest

import backend.db as db
from backend.db.datasets import resolve
from backend.db.loghub import status

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_OPENSTACK_LIVE") != "1",
    reason="set RUN_OPENSTACK_LIVE=1 and load OpenStack to run these",
)

# Covers the whole corpus with margin; measured span below.
WINDOW = ("2017-05-14 00:00:00+00", "2017-05-18 00:00:00+00")


@pytest.fixture(scope="module")
def loaded():
    state = status("openstack")
    if not state.get("exists") or not state.get("rows"):
        pytest.skip("OpenStack is not loaded")
    return state


# Every line of the three raw nova logs (52,312 + 137,074 + 18,434), all of which
# the parser accepts. Asserted exactly: a loader that silently drops rows is
# the failure mode that matters, and it is invisible in every aggregate.
OPENSTACK_ROWS = 207_820

# Measured by scanning the three raw files.
SEVERITY_COUNTS = {"INFO": 204_506, "WARNING": 3_118, "ERROR": 194, "CRITICAL": 2}


def test_the_corpus_loaded_completely(loaded):
    assert loaded["rows"] == OPENSTACK_ROWS


def test_the_corpus_spans_the_three_runs(loaded):
    """First line 2017-05-14 19:39:01.445, last 2017-05-17 12:02:35.320 (UTC)."""
    assert loaded["covered_start"].startswith("2017-05-14 19:39:01")
    assert loaded["covered_end"].startswith("2017-05-17 12:02:35")


def test_line_order_is_time_order(loaded):
    """The three files load in chronological order, so no line is older than
    the one before it. Loaded normal1, normal2, abnormal, exactly one step went
    back (two days, at the join to the abnormal run)."""
    from backend.db.connection import db_cursor

    with db_cursor() as cur:
        cur.execute("""
            WITH o AS (
                SELECT "Timestamp" AS ts,
                       LAG("Timestamp") OVER (ORDER BY "LineId") AS prev
                FROM openstack
            )
            SELECT count(*) FILTER (WHERE ts < prev) AS backward FROM o
        """)
        assert cur.fetchone()["backward"] == 0

    first = db.get_log_by_record_id(1, dataset="openstack")
    last = db.get_log_by_record_id(OPENSTACK_ROWS, dataset="openstack")
    assert str(first[0]["ts"]).startswith("2017-05-14 19:39:01")
    assert str(last[0]["ts"]).startswith("2017-05-17 12:02:35")


def test_severity_counts_are_exact_and_uppercase(loaded):
    rows = db.count_by_field(*WINDOW, dataset="openstack")
    assert not rows.refused
    assert rows.stats.approximate is False

    counts = {r["severity"]: r["count"] for r in rows}
    assert all(sev.isupper() for sev in counts)
    assert sum(counts.values()) == OPENSTACK_ROWS
    assert counts == SEVERITY_COUNTS


def test_components_rank_by_volume(loaded):
    rows = db.top_by_field(*WINDOW, dataset="openstack", field="service")
    assert not rows.refused
    services = [r["service"] for r in rows]
    # Third by volume (24,784 rows) behind the two wsgi/imagecache components.
    assert "nova.compute.manager" in services


def test_an_hour_bucketed_trend_covers_the_whole_corpus(loaded):
    """About 65 hours: hour buckets fit under the cap, minute buckets do not."""
    rows = db.get_trend(
        *WINDOW, "service", "nova.compute.manager", dataset="openstack", bucket="hour"
    )
    assert not rows.refused
    assert 1 < len(rows) <= 96


def test_a_minute_bucketed_full_range_trend_is_refused(loaded):
    rows = db.get_trend(
        *WINDOW, "service", "nova.compute.manager", dataset="openstack", bucket="minute"
    )
    assert rows.refused
    assert rows.refusal.code == "too_many_buckets"


def test_the_trend_lists_every_bucket_including_the_silent_ones(loaded):
    """The three runs leave gaps (05-14 21:27 to 05-16 00:00, 05-16 06:25 to
    15:15). Each hour of the window is a row, the gaps are 0, and the counts still
    add up to the measured total."""
    rows = db.get_trend(
        *WINDOW, "service", "nova.compute.manager", dataset="openstack", bucket="hour"
    )
    assert not rows.refused
    assert len(rows) == 96  # 2017-05-14 00:00 .. 2017-05-18 00:00, hourly
    buckets = [str(r["bucket"]) for r in rows]
    assert buckets == sorted(buckets)
    assert any(r["count"] == 0 for r in rows)
    # 2017-05-15 is between runs: every one of its 24 hours is an empty bucket.
    assert all(r["count"] == 0 for r in rows if str(r["bucket"]).startswith("2017-05-15"))

    totals = db.top_by_field(*WINDOW, field="service", dataset="openstack", limit=100)
    measured = next(r["count"] for r in totals if r["service"] == "nova.compute.manager")
    assert sum(r["count"] for r in rows) == measured


def test_an_omitted_bucket_is_chosen_from_the_window(loaded):
    rows = db.get_trend(*WINDOW, "service", "nova.compute.manager", dataset="openstack")
    assert not rows.refused
    assert len(rows) == 96
    assert any("hour (chosen from the window length)" in n for n in rows.stats.notes)


def test_a_per_severity_trend_has_one_zero_filled_series_per_level(loaded):
    rows = db.get_trend(
        *WINDOW, "service", "nova.compute.manager", dataset="openstack",
        bucket="day", group_by="severity",
    )
    assert not rows.refused
    levels = {r["severity"] for r in rows}
    days = {str(r["bucket"]) for r in rows}
    assert len(days) == 4
    # Every (day, level) pair exists, 0 where that level was silent.
    assert len(rows) == len(days) * len(levels)
    by_level: dict[str, int] = {}
    for r in rows:
        by_level[r["severity"]] = by_level.get(r["severity"], 0) + r["count"]
    counted = db.count_by_field(*WINDOW, group_by="service", dataset="openstack", limit=200)
    expected = {
        r["severity"]: r["count"] for r in counted if r["service"] == "nova.compute.manager"
    }
    assert by_level == expected


def test_lexical_search_finds_an_exact_phrase(loaded):
    rows = db.search_logs("Instance spawned successfully", dataset="openstack", limit=5)
    assert len(rows) > 0
    assert any("spawned" in str(r["message"]).lower() for r in rows)


def test_fetch_by_record_id_returns_one_row(loaded):
    rows = db.get_log_by_record_id(1, dataset="openstack")
    assert len(rows) == 1
    assert rows[0]["record_id"] == 1


def test_every_selector_returns_suggestions(loaded):
    """Every offered selector, since a column missing from the token index fails
    only at query time. No '#' (no event ids) and no '!' (only the case
    modifier, never a selector). Checked per corpus through the private
    lookup, since the merged one hides which corpus answered."""
    from backend.db.auto_complete import _auto_complete_dataset

    for query in ("@nova", "^Took", "$spawned"):
        suggestions = _auto_complete_dataset(query, dataset="openstack")
        assert suggestions, query

    assert "#" not in resolve("openstack").selectors
    assert "!" not in resolve("openstack").selectors


def test_the_merged_type_ahead_includes_openstack(loaded):
    suggestions = db.auto_complete("@nova")
    assert suggestions
    assert any(str(s["suggestion"]).startswith("nova.") for s in suggestions)


def test_the_corpus_stays_well_inside_its_allowance(loaded):
    from backend.db.budget import ALLOWANCES
    from backend.db.probe import probe_cursor

    with probe_cursor() as cur:
        cur.execute("SELECT pg_total_relation_size('openstack') AS b")
        size = int(cur.fetchone()["b"])

    assert size < ALLOWANCES["openstack"]


# --- Dataset catalog (list_datasets_tool) -------------------------------------
def _catalog_row(dataset_id):
    return next(r for r in db.list_datasets() if r["dataset"] == dataset_id)


def test_the_catalog_reports_the_loaded_corpus_exactly(loaded):
    row = _catalog_row("openstack")
    assert row["available"] is True and row["rows_exact"] is True
    assert row["rows"] == OPENSTACK_ROWS
    assert {lv["severity"]: lv["count"] for lv in row["levels"]} == SEVERITY_COUNTS
    assert str(row["first_timestamp"]).startswith("2017-05-14 19:39:01")
    assert str(row["last_timestamp"]).startswith("2017-05-17 12:02:35")
    assert row["whole_span_fits_one_window"] is True


def test_the_catalog_window_covers_every_row(loaded):
    """window_start..window_end is the agent's whole-dataset window: one count
    over it must see every row, so the end really is past the last one."""
    row = _catalog_row("openstack")
    rows = db.count_by_field(row["window_start"], row["window_end"], dataset="openstack")
    assert not rows.refused
    assert sum(r["count"] for r in rows) == row["rows"]
