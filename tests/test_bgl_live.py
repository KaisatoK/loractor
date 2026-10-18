"""Live BGL checks against a loaded corpus.

Gated by ``RUN_BGL_LIVE=1`` because they need the corpus loaded:

    bash ./setup.sh --embed bgl    # load BGL + embed templates
    RUN_BGL_LIVE=1 uv run pytest tests/test_bgl_live.py

Kept out of the default suite so a fresh checkout does not fail for want of a
corpus nobody asked for. The offline proof that the registry generalises lives
in ``test_bgl.py``; this file confirms the queries actually return data.
"""

from __future__ import annotations

import os

import pytest

import backend.db as db
from backend.db.config import EMBEDDING_MODEL
from backend.db.datasets import resolve
from backend.db.loghub import status
from backend.db.templates import matched_templates, templates_status

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_BGL_LIVE") != "1",
    reason="set RUN_BGL_LIVE=1 and load BGL to run these",
)

WINDOW = ("2005-06-01 00:00:00+00", "2006-02-01 00:00:00+00")


@pytest.fixture(scope="module")
def loaded():
    state = status("bgl")
    if not state.get("exists") or not state.get("rows"):
        pytest.skip("BGL is not loaded")
    return state


EMBEDDING_SECRET = "local_ai"


@pytest.fixture(scope="module")
def embedding_secret():
    """Skip the cases that call ``ai_embed`` when SereneDB has no ``local_ai`` secret.

    Search is lexical (BM25) by default, so the secret only exists for the opt-in
    template embeddings. ``ai_embed`` looks the secret up before it sends anything,
    so a NULL text makes this a metadata check that costs no request. Only the
    "secret not found" error skips; any other failure propagates rather than being
    read as a missing secret.
    """
    import psycopg2

    from backend.db.probe import probe_cursor

    try:
        with probe_cursor() as cur:
            cur.execute(
                "SELECT ai_embed(NULL, %s, %s)", [EMBEDDING_MODEL, EMBEDDING_SECRET]
            )
    except psycopg2.Error as exc:
        if f"secret '{EMBEDDING_SECRET}' not found" in str(exc):
            pytest.skip(
                f"SereneDB secret '{EMBEDDING_SECRET}' is not configured "
                "(needed for ai_embed)"
            )
        raise


# Every line of the published BGL log. Asserted exactly, because a loader that
# silently drops rows is the failure mode that matters here -- an early parser
# dropped 34,470 of them (all fields present, empty message, mostly FATAL) and
# the loss was invisible in every aggregate.
BGL_ROWS = 4_747_963


def test_the_corpus_loaded_completely(loaded):
    assert loaded["rows"] == BGL_ROWS


def test_the_corpus_spans_seven_months(loaded):
    """Its distinguishing property, and the reason week/month buckets exist."""
    assert loaded["covered_start"].startswith("2005-06")
    assert loaded["covered_end"].startswith("2006-01")


def test_severity_counts_are_exact_and_uppercase(loaded):
    rows = db.count_by_field(*WINDOW, dataset="bgl")
    assert not rows.refused
    assert rows.stats.approximate is False

    counts = {r["severity"]: r["count"] for r in rows}
    assert sum(counts.values()) == BGL_ROWS
    assert all(sev.isupper() for sev in counts)
    assert "INFO" in counts
    # FAILURE is absent from loghub's 2,000-line sample but real in the full
    # corpus; a vocabulary taken from the sample refused these rows as unknown.
    assert counts["FAILURE"] == 1_714


def test_components_rank_by_volume(loaded):
    rows = db.top_by_field(*WINDOW, dataset="bgl", field="service")
    assert not rows.refused
    services = [r["service"] for r in rows]
    assert "KERNEL" in services


def test_a_month_bucketed_trend_covers_the_whole_corpus(loaded):
    rows = db.get_trend(
        *WINDOW, "service", "KERNEL", dataset="bgl", bucket="month"
    )
    assert not rows.refused
    assert 1 < len(rows) <= 12


def test_a_day_bucketed_full_range_trend_is_refused(loaded):
    """245 buckets exceeds the cap; the refusal must point somewhere useful."""
    rows = db.get_trend(*WINDOW, "service", "KERNEL", dataset="bgl", bucket="day")
    assert rows.refused
    assert rows.refusal.code == "too_many_buckets"
    assert "week" in rows.refusal.suggestion


def test_minute_trend_reports_non_zero_burst_runs(loaded):
    from backend.db import add_burst_summary

    rows = db.get_trend(
        "2005-06-05 07:00:00+00", "2005-06-05 08:00:00+00",
        "service", "KERNEL", dataset="bgl", bucket="minute",
        severity="FATAL",
    )
    assert not rows.refused
    add_burst_summary(rows)
    note = next(note for note in rows.stats.notes if "non-zero bucket runs:" in note)
    assert "2005-06-05 07:08:00Z–2005-06-05 07:10:00Z (76 log entries)" in note
    assert "2005-06-05 07:35:00Z–2005-06-05 07:37:00Z (76 log entries)" in note
    assert "2005-06-05 07:47:00Z–2005-06-05 07:49:00Z (54 log entries)" in note


def test_lexical_search_finds_an_exact_phrase(loaded):
    rows = db.search_logs("cache parity error", dataset="bgl", limit=5)
    assert len(rows) > 0
    assert any("parity" in str(r["message"]).lower() for r in rows)


def test_semantic_search_finds_what_shares_no_words(loaded, embedding_secret):
    """'memory hardware fault' matches kernel/DDR failures by meaning."""
    if not templates_status("bgl").get("exists"):
        pytest.skip("BGL template embeddings not built")

    rows = db.search_logs("memory hardware fault", dataset="bgl", limit=5)
    assert len(rows) > 0
    assert any("template-level" in note for note in rows.stats.notes)


def test_template_embeddings_cover_the_whole_corpus(loaded):
    state = templates_status("bgl")
    if not state.get("exists"):
        pytest.skip("BGL template embeddings not built")
    assert state["complete"]
    assert state["unembedded"] == 0 if "unembedded" in state else True

    # 34,470 rows carry no message, so they have no template to embed. They
    # stay queryable by every other means; only semantic search cannot reach
    # them, and the shortfall is exactly that count rather than a silent gap.
    assert state["rows_covered"] == BGL_ROWS - 34_470


def test_templates_collapse_the_corpus(loaded):
    """20,352 templates for 4.7M rows: a 233x reduction.

    That ratio is what makes semantic search possible at all here. Embedding
    every row would be ~110 hours of calls; embedding every template took 36
    minutes.
    """
    state = templates_status("bgl")
    if not state.get("exists"):
        pytest.skip("BGL template embeddings not built")

    assert state["templates"] < BGL_ROWS / 100
    assert state["rows_covered"] / state["templates"] > 100


def test_matched_templates_carry_their_row_counts(loaded, embedding_secret):
    if not templates_status("bgl").get("exists"):
        pytest.skip("BGL template embeddings not built")

    matches = matched_templates("kernel panic", "bgl", limit=3)
    assert matches
    assert all(m["row_count"] > 0 for m in matches)


def test_fetch_by_record_id_returns_one_row(loaded):
    rows = db.get_log_by_record_id(1, dataset="bgl")
    assert len(rows) == 1
    assert rows[0]["record_id"] == 1


def test_every_selector_returns_suggestions(loaded):
    """Every offered selector, since a column missing from the token index fails
    only at query time."""
    # Three selectors: there is no event-id selector ('?'), and '!' is only the
    # case modifier, never a selector. Checked per corpus through the private
    # lookup, since the merged one hides which corpus answered.
    from backend.db.auto_complete import _auto_complete_dataset

    for query in ("@KERN", "#instruction", "$parity"):
        suggestions = _auto_complete_dataset(query, dataset="bgl")
        assert suggestions, query

    assert "?" not in resolve("bgl").selectors
    assert "!" not in resolve("bgl").selectors


def test_the_corpus_stays_well_inside_its_allowance(loaded):
    from backend.db.budget import ALLOWANCES
    from backend.db.probe import probe_cursor

    with probe_cursor() as cur:
        cur.execute("SELECT pg_total_relation_size('bgl') AS b")
        size = int(cur.fetchone()["b"])

    assert size < ALLOWANCES["bgl"]


# --- Dataset catalog (list_datasets_tool) -------------------------------------
def test_the_catalog_reports_the_loaded_corpus_exactly(loaded):
    row = next(r for r in db.list_datasets() if r["dataset"] == "bgl")
    assert row["available"] is True and row["rows_exact"] is True
    assert row["rows"] == BGL_ROWS
    levels = {lv["severity"]: lv["count"] for lv in row["levels"]}
    assert sum(levels.values()) == BGL_ROWS
    assert levels["FAILURE"] == 1_714
    assert row["whole_span_fits_one_window"] is True

    # The catalog's own window, used as a tool would use it, sees every row.
    rows = db.count_by_field(row["window_start"], row["window_end"], dataset="bgl")
    assert not rows.refused
    assert sum(r["count"] for r in rows) == BGL_ROWS


# --- Whole-dataset counts (count_by_dataset_tool) ------------------------------
def test_counts_by_dataset_cover_every_row_of_each_local_corpus(loaded):
    rows = db.count_by_dataset()
    if rows.refused:
        pytest.skip(f"not every local corpus is loaded: {rows.refusal.reason}")
    totals: dict[str, int] = {}
    for row in rows:
        totals[row["dataset"]] = totals.get(row["dataset"], 0) + row["count"]
    assert totals["bgl"] == BGL_ROWS
    # Agrees with the catalog's own exact profile of every local dataset.
    catalog = {r["dataset"]: r for r in db.list_datasets()}
    for dataset_id, total in totals.items():
        assert total == catalog[dataset_id]["rows"]
    fatal = next(r for r in rows if r["dataset"] == "bgl" and r["severity"] == "FATAL")
    assert fatal["count"] == 855_501


# --- Time-filtered search with an exact count (T1) -----------------------------
def test_a_rare_severity_is_found_and_counted_inside_a_window(loaded):
    """Before T1, 'error' at FATAL returned none of its 345,086 matches: the
    top 100 by BM25 were all other levels. Severity and the window now apply
    inside the ranked branch, and the count uses the same conditions."""
    from backend.db.connection import db_cursor
    from backend.db.query import search_with_total

    lo, hi = "2005-07-01 00:00:00", "2005-08-01 00:00:00"
    rows = search_with_total("error", "bgl", "FATAL", 10, lo, hi)
    assert not rows.refused
    assert len(rows) == 10
    for row in rows:
        assert row["severity"] == "FATAL"
        assert lo <= row["ts"].strftime("%Y-%m-%d %H:%M:%S") < hi

    with db_cursor() as cur:
        cur.execute(
            'SELECT count(*) AS n FROM bgl_idx i JOIN bgl t ON t."LineId" = i."LineId" '
            'WHERE i."Content" @@ ts_any([ts_levenshtein(%s, 1)]) AND t."Level" = %s '
            'AND t."Timestamp" >= %s::timestamptz AND t."Timestamp" < %s::timestamptz',
            ["error", "FATAL", lo, hi],
        )
        assert rows.total_matches == int(cur.fetchone()["n"])


# --- Nearby events (get_nearby_events_tool) ------------------------------------
def test_nearby_events_are_contiguous_and_numbered_outward(loaded):
    anchor = BGL_ROWS // 2
    rows = db.get_nearby_events(anchor, "bgl", before=5, after=5)
    assert not rows.refused
    assert [r["record_id"] for r in rows] == list(range(anchor - 5, anchor + 6))
    assert [r["offset"] for r in rows] == list(range(-5, 6))
    assert "request_id" not in rows[0]  # BGL records no request id


def test_same_entity_follows_the_anchors_node(loaded):
    from backend.db.connection import db_cursor

    anchor = BGL_ROWS // 2
    with db_cursor() as cur:
        cur.execute('SELECT "Node" FROM bgl WHERE "LineId" = %s', [anchor])
        node = cur.fetchone()["Node"]
    rows = db.get_nearby_events(anchor, "bgl", before=5, after=5, same_entity=True)
    assert {r["entity_id"] for r in rows} == {node}
    ids = [r["record_id"] for r in rows]
    assert ids == sorted(ids) and anchor in ids
    assert rows.stats.elapsed_ms < 5_000


def test_a_node_seen_once_returns_the_anchor_alone_with_a_note(loaded):
    from backend.db.connection import db_cursor

    with db_cursor() as cur:
        cur.execute("""
            SELECT min("LineId") AS line FROM bgl WHERE "Node" IN (
                SELECT "Node" FROM bgl GROUP BY "Node" HAVING count(*) = 1)
        """)
        anchor = cur.fetchone()["line"]
    if anchor is None:
        pytest.skip("every BGL node logs more than one line")
    rows = db.get_nearby_events(anchor, "bgl", same_entity=True)
    assert [r["record_id"] for r in rows] == [anchor]
    assert "No other line in 'bgl' shares the anchor's entity_id." in rows.stats.notes


def test_same_request_is_refused_on_bgl(loaded):
    rows = db.get_nearby_events(1, "bgl", same_request=True)
    assert rows.refused and rows.refusal.code == "unsupported_field"


# --- Team ownership counts (T3) ----------------------------------------------
def test_team_counts_preserve_every_bgl_row(loaded):
    rows = db.count_by_team(datasets=("bgl",))
    if rows.refused:
        pytest.skip(f"team_ownership is not loaded: {rows.refusal.reason}")
    by_severity = {}
    for row in rows:
        by_severity[row["severity"]] = by_severity.get(row["severity"], 0) + row["count"]
    whole = db.count_by_dataset(("bgl",))
    expected = {row["severity"]: row["count"] for row in whole}
    assert by_severity == expected
    assert not any(row["team"] == "unowned" for row in rows)


def test_windowed_team_counts_are_bounded(loaded):
    rows = db.count_by_team(
        datasets=("bgl",),
        start_time="2005-07-01 00:00:00",
        end_time="2005-08-01 00:00:00",
    )
    if rows.refused:
        pytest.skip(f"team_ownership is not loaded: {rows.refusal.reason}")
    assert sum(row["count"] for row in rows) <= BGL_ROWS


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

    row = next(r for r in db.list_datasets() if r["dataset"] == dataset_id)
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
    lo, mid, hi = _halves("bgl")
    result = _rank(
        datasets=["bgl"], group_by=group_by,
        baseline_start=lo, baseline_end=mid, recent_start=mid, recent_end=hi,
    )
    envelope = result.structured_content
    if envelope["status"] == "refused":
        pytest.skip(envelope["reason"])
    assert envelope["kind"] == "derived"
    assert envelope["data"]
    expected = _direct_counts("bgl", group_by, lo, mid, hi)
    for row in envelope["data"]:
        value = str(row[group_by])
        assert row["non_info_rows"] == expected[value]["non_info"]
        assert row["error_rows"] == expected[value]["error"]
