"""Live OpenStack checks against a loaded corpus.

Gated by ``RUN_OPENSTACK_LIVE=1`` because they need the corpus loaded:

    bash ./setup.sh openstack
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
    only at query time. No '?' (no event ids) and no '!' (only the case
    modifier, never a selector). Checked per corpus through the private
    lookup, since the merged one hides which corpus answered."""
    from backend.db.auto_complete import _auto_complete_dataset

    for query in ("@nova", "#Took", "$spawned"):
        suggestions = _auto_complete_dataset(query, dataset="openstack")
        assert suggestions, query

    assert "?" not in resolve("openstack").selectors
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


# --- Time-filtered search with an exact count (T1) -----------------------------
def _independent_count(term, severity=None, lo=None, hi=None):
    """Matches counted with plain SQL, not through the search code."""
    from backend.db.connection import db_cursor

    where = ['i."Content" @@ ts_any([ts_levenshtein(%s, 1)])']
    params = [term]
    if severity:
        where.append('t."Level" = %s')
        params.append(severity)
    if lo:
        where.append('t."Timestamp" >= %s::timestamptz AND t."Timestamp" < %s::timestamptz')
        params += [lo, hi]
    with db_cursor() as cur:
        cur.execute(
            'SELECT count(*) AS n FROM openstack_idx i JOIN openstack t '
            f'ON t."LineId" = i."LineId" WHERE {" AND ".join(where)}',
            params,
        )
        return int(cur.fetchone()["n"])


def test_a_severity_ranked_below_the_top_100_is_still_found(loaded):
    """Before T1, severity was applied after the BM25 top-100 cut: 'instance'
    has 101,982 matches, its 24 ERROR rows rank below the first 100, and the
    search returned none of them."""
    from backend.db.query import search_with_total

    rows = search_with_total("instance", "openstack", "ERROR", 10)
    assert not rows.refused
    assert rows.total_matches == _independent_count("instance", "ERROR") == 24
    assert len(rows) == 10
    assert all(r["severity"] == "ERROR" for r in rows)
    assert rows.total_matches > len(rows)  # so `truncated` will be true


def test_a_windowed_search_returns_only_rows_inside_the_window(loaded):
    from backend.db.query import search_with_total

    lo, hi = "2017-05-16 00:00:00", "2017-05-16 06:00:00"
    rows = search_with_total("instance", "openstack", "ERROR", 10, lo, hi)
    assert not rows.refused
    assert rows.total_matches == _independent_count("instance", "ERROR", lo, hi)
    assert len(rows) == min(10, rows.total_matches)
    for row in rows:
        assert row["severity"] == "ERROR"
        assert lo <= row["ts"].strftime("%Y-%m-%d %H:%M:%S") < hi


def test_the_total_is_exact_below_the_limit(loaded):
    """'failed' matches only 10 rows: all are returned and nothing is cut."""
    from backend.db.query import search_with_total

    rows = search_with_total("failed", "openstack", None, 50)
    assert rows.total_matches == _independent_count("failed") == len(rows)


# --- Nearby events (get_nearby_events_tool) ------------------------------------
# Anchors are found by query, never hard-coded, so the checks hold for any load.
def _one(sql_text: str, params: list | None = None):
    from backend.db.connection import db_cursor

    with db_cursor() as cur:
        cur.execute(sql_text, params or [])
        row = cur.fetchone()
    return row and next(iter(row.values()))


def test_nearby_events_are_contiguous_and_numbered_outward(loaded):
    anchor = OPENSTACK_ROWS // 2
    rows = db.get_nearby_events(anchor, "openstack", before=5, after=5)
    assert not rows.refused and rows.stats.notes == []
    assert [r["record_id"] for r in rows] == list(range(anchor - 5, anchor + 6))
    assert [r["offset"] for r in rows] == list(range(-5, 6))
    # The anchor row is the line itself, with every canonical column.
    middle = rows[5]
    assert middle == {**middle, **db.get_log_by_record_id(anchor, "openstack")[0]}
    assert {"request_id", "template", "entity_id"} <= set(middle)


def test_nearby_events_stop_at_the_ends_of_the_table(loaded):
    first = db.get_nearby_events(1, "openstack", before=5, after=2)
    assert [r["offset"] for r in first] == [0, 1, 2]
    last = db.get_nearby_events(OPENSTACK_ROWS, "openstack", before=2, after=5)
    assert [r["offset"] for r in last] == [-2, -1, 0]


def test_same_entity_follows_the_anchors_process(loaded):
    anchor = OPENSTACK_ROWS // 2
    rows = db.get_nearby_events(anchor, "openstack", before=5, after=5,
                                same_entity=True)
    pid = _one('SELECT "Pid" FROM openstack WHERE "LineId" = %s', [anchor])
    assert len(rows) == 11
    assert {r["entity_id"] for r in rows} == {pid}
    ids = [r["record_id"] for r in rows]
    assert ids == sorted(ids) and ids[5] == anchor


def test_same_request_follows_one_request(loaded):
    """An anchor inside the busiest request: every neighbour shares its id, and
    the derived request_id is exactly the first word of ADDR. A request's lines
    are interleaved with other requests', so the anchor is its own line, not
    an offset from the request's first line."""
    request = _one("""
        SELECT r FROM (
            SELECT CASE WHEN left("ADDR", 4) = 'req-'
                        THEN regexp_replace("ADDR", ' .*$', '') END AS r
            FROM openstack) x
        WHERE r IS NOT NULL GROUP BY r ORDER BY count(*) DESC LIMIT 1
    """)
    anchor = _one(
        """SELECT "LineId" FROM openstack
           WHERE split_part("ADDR", ' ', 1) = %s ORDER BY "LineId" OFFSET 100 LIMIT 1""",
        [request],
    )
    rows = db.get_nearby_events(anchor, "openstack", before=5, after=5,
                                same_request=True)
    assert not rows.refused
    assert len(rows) == 11 and rows[5]["record_id"] == anchor
    assert {r["request_id"] for r in rows} == {request}
    assert any("request context summary:" in note for note in rows.stats.notes)
    assert request.startswith("req-") and " " not in request
    # The filter does skip the other requests' lines in between.
    span = rows[-1]["record_id"] - rows[0]["record_id"] + 1
    unfiltered = db.get_nearby_events(anchor, "openstack", before=5, after=5)
    assert span >= len(rows) and len(unfiltered) == 11


def test_same_request_on_a_line_outside_any_request_returns_the_anchor(loaded):
    anchor = _one('SELECT min("LineId") FROM openstack WHERE "ADDR" = %s', ["-"])
    rows = db.get_nearby_events(anchor, "openstack", same_request=True)
    assert [r["record_id"] for r in rows] == [anchor]
    assert rows[0]["request_id"] is None
    assert "has no request_id" in rows.stats.notes[0]


def test_an_unknown_anchor_is_empty_with_a_note(loaded):
    rows = db.get_nearby_events(OPENSTACK_ROWS + 1, "openstack")
    assert not rows.refused and list(rows) == []
    assert f"No line has record_id {OPENSTACK_ROWS + 1}" in rows.stats.notes[0]


def test_the_nearby_events_tool_returns_its_query(loaded):
    from backend.pipeline.tools import get_nearby_events_tool

    class _Runtime:
        events: list = []
        stream_writer = events.append

    envelope = get_nearby_events_tool.func(
        line_id=OPENSTACK_ROWS // 2, runtime=_Runtime(), dataset="openstack",
        before=2, after=2, same_entity=True, same_request=False,
    )
    assert envelope["status"] == "success" and envelope["kind"] == "context"
    assert envelope["returned"] == 5 and envelope["truncated"] is False
    assert "WITH anchor AS" in envelope["query"]
    assert envelope["filters"]["same_entity"] is True


# --- Team ownership counts (T3) ----------------------------------------------
def test_team_counts_preserve_every_openstack_row(loaded):
    rows = db.count_by_team(datasets=("openstack",))
    if rows.refused:
        pytest.skip(f"team_ownership is not loaded: {rows.refusal.reason}")
    by_severity = {}
    for row in rows:
        by_severity[row["severity"]] = by_severity.get(row["severity"], 0) + row["count"]
    whole = db.count_by_dataset(("openstack",))
    expected = {row["severity"]: row["count"] for row in whole}
    assert by_severity == expected
    assert not any(row["team"] == "unowned" for row in rows)


def test_windowed_team_counts_are_bounded(loaded):
    rows = db.count_by_team(
        datasets=("openstack",),
        start_time="2017-05-16 00:00:00",
        end_time="2017-05-17 00:00:00",
    )
    if rows.refused:
        pytest.skip(f"team_ownership is not loaded: {rows.refusal.reason}")
    assert sum(row["count"] for row in rows) <= OPENSTACK_ROWS


# --- Rank priorities (T13, server-only tool) -----------------------------------
def _rank(**arguments):
    import asyncio

    from fastmcp import Client

    from backend.mcp_server import server

    async def go():
        async with Client(server.mcp) as client:
            return await client.call_tool("rank_priorities_tool", arguments)

    return asyncio.run(go())


def _halves(dataset_id):
    """The two halves of the dataset's own window, from list_datasets_tool."""
    from datetime import datetime

    row = _catalog_row(dataset_id)
    start = datetime.strptime(row["window_start"], "%Y-%m-%d %H:%M:%S")
    end = datetime.strptime(row["window_end"], "%Y-%m-%d %H:%M:%S")
    middle = start + (end - start) / 2
    fmt = "%Y-%m-%d %H:%M:%S"
    return start.strftime(fmt), middle.strftime(fmt), end.strftime(fmt)


def _direct_counts(dataset, group_by, lo, mid, hi):
    """Non-INFO and severe counts per value, from the same 200-group queries."""
    from backend.pipeline.priority import INFO_LEVELS, SEVERE_LEVELS
    from backend.pipeline.ranking import CANDIDATE_GROUPS

    totals: dict[str, dict[str, int]] = {}
    for start, end in ((lo, mid), (mid, hi)):
        if group_by == "team":
            rows = db.count_by_team(datasets=(dataset,), start_time=start, end_time=end)
        else:
            rows = db.count_by_field(start, end, group_by, dataset, None, CANDIDATE_GROUPS)
        assert not rows.refused
        for row in rows:
            if group_by == "team" and row["dataset"] != dataset:
                continue
            severity = str(row["severity"]).upper()
            if severity in INFO_LEVELS:
                continue
            bucket = totals.setdefault(str(row[group_by]), {"non_info": 0, "error": 0})
            bucket["non_info"] += int(row["count"])
            if severity in SEVERE_LEVELS:
                bucket["error"] += int(row["count"])
    return totals


@pytest.mark.parametrize("group_by", ["service", "template", "team"])
def test_rank_priorities_rows_equal_the_direct_count_sums(loaded, group_by):
    lo, mid, hi = _halves("openstack")
    result = _rank(
        datasets=["openstack"], group_by=group_by,
        baseline_start=lo, baseline_end=mid, recent_start=mid, recent_end=hi,
    )
    envelope = result.structured_content
    if envelope["status"] == "refused":
        pytest.skip(envelope["reason"])
    assert envelope["kind"] == "derived"
    assert envelope["data"]
    expected = _direct_counts("openstack", group_by, lo, mid, hi)
    for row in envelope["data"]:
        value = str(row[group_by])
        assert row["non_info_rows"] == expected[value]["non_info"]
        assert row["error_rows"] == expected[value]["error"]
    assert envelope["total_matches"] >= len(envelope["data"])
