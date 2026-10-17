"""Live BGL checks against a loaded corpus.

Gated by ``RUN_BGL_LIVE=1`` because they need the corpus loaded:

    uv run python -m backend.db.loghub            # load + embed templates
    RUN_BGL_LIVE=1 uv run pytest tests/test_bgl_live.py

Kept out of the default suite so a fresh checkout does not fail for want of a
corpus nobody asked for. The offline proof that the registry generalises lives
in ``test_bgl.py``; this file confirms the queries actually return data.
"""

from __future__ import annotations

import os

import pytest

import backend.db as db
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


def test_lexical_search_finds_an_exact_phrase(loaded):
    rows = db.search_logs("cache parity error", dataset="bgl", limit=5)
    assert len(rows) > 0
    assert any("parity" in str(r["message"]).lower() for r in rows)


def test_semantic_search_finds_what_shares_no_words(loaded):
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


def test_matched_templates_carry_their_row_counts(loaded):
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
    # Three selectors: the raw corpus carries no event ids, so '#' is not
    # offered, and '!' is only the case modifier, never a selector. Checked per
    # corpus through the private lookup, since the merged one hides which
    # corpus answered.
    from backend.db.auto_complete import _auto_complete_dataset

    for query in ("@KERN", "^instruction", "$parity"):
        suggestions = _auto_complete_dataset(query, dataset="bgl")
        assert suggestions, query

    assert "#" not in resolve("bgl").selectors
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
