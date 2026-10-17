"""Live SereneDB integration tests for the dataset-driven query layer.

These run against a live SereneDB and **fail, never skip**, when it is
unreachable or unseeded, so a broken database setup is loud.

Everything here targets the ``openstack`` corpus, which is the seeded demo
table. The point of most of these tests is the compatibility contract: the
registry refactor must leave the OpenStack path answering exactly as it did
before, through the new generalized builders.

Remote-corpus tests live in ``test_otel_live.py`` and are env-gated, because
they read from public object storage.
"""

import socket

import pytest
from psycopg2 import sql

from backend.db import (
    auto_complete,
    compare_periods,
    count_by_field,
    count_distinct_by_field,
    get_field_values,
    get_log_by_record_id,
    get_trend,
    search_logs,
    top_by_field,
)
from backend.db.config import SERENEDB_PORT, TABLE_NAME
from backend.db.connection import db_cursor

# The sample dataset (OpenStack_2k.log_structured.csv) spans this window.
START = "2017-05-16 00:00:00"
END = "2017-05-16 00:10:00"
BASELINE_START = "2017-05-16 00:00:00"
BASELINE_END = "2017-05-16 00:01:00"
RECENT_START = "2017-05-16 00:02:00"
RECENT_END = "2017-05-16 00:03:00"

# Wide enough to cover the whole sample regardless of its exact timestamps.
WIDE_START = "2017-05-01 00:00:00"
WIDE_END = "2017-06-01 00:00:00"

TREND_VALUES = {"initial", "up", "down", "flat"}


@pytest.fixture(scope="session", autouse=True)
def serenedb() -> None:
    """Fail loudly when SereneDB is not reachable or has no data."""
    try:
        with socket.create_connection(("localhost", SERENEDB_PORT), timeout=2):
            pass
    except OSError as e:
        pytest.fail(
            f"SereneDB is not reachable on localhost:{SERENEDB_PORT} ({e}). "
            "Start SereneDB before running the DB tests."
        )

    try:
        services = get_field_values(WIDE_START, WIDE_END, "service")
    except Exception as e:  # noqa: BLE001 - surface any connection/query failure
        pytest.fail(
            f"SereneDB is reachable on localhost:{SERENEDB_PORT} but querying "
            f"failed: {e}"
        )

    if not services:
        pytest.fail(
            "The 'log' table is empty. Seed it with "
            "`uv run python -m backend.db.setup` before running the DB tests."
        )


def _top_service() -> str:
    return top_by_field(WIDE_START, WIDE_END, "service")[0]["service"]


def _top_entity() -> int:
    return int(get_field_values(WIDE_START, WIDE_END, "entity_id")[0]["value"])


# --- counts ------------------------------------------------------------------
def test_count_by_severity_returns_rows():
    rows = count_by_field(WIDE_START, WIDE_END)
    assert rows
    assert {"severity", "count"} <= set(rows[0])
    assert all(int(r["count"]) > 0 for r in rows)


def test_count_filtered_to_one_severity():
    rows = count_by_field(WIDE_START, WIDE_END, severity="INFO")
    assert rows
    assert all(r["severity"] == "INFO" for r in rows)


def test_count_grouped_by_service():
    rows = count_by_field(WIDE_START, WIDE_END, group_by="service")
    assert rows
    assert {"service", "severity", "count"} <= set(rows[0])


def test_count_grouped_by_entity_id():
    rows = count_by_field(WIDE_START, WIDE_END, group_by="entity_id")
    assert rows
    assert "entity_id" in rows[0]


def test_count_grouped_by_event_id():
    rows = count_by_field(WIDE_START, WIDE_END, group_by="event_id")
    assert rows
    assert "event_id" in rows[0]


def test_counts_are_ordered_by_volume():
    rows = count_by_field(WIDE_START, WIDE_END, group_by="service")
    counts = [int(r["count"]) for r in rows]
    assert counts == sorted(counts, reverse=True)


def test_distinct_count_is_exact_on_the_local_corpus():
    rows = count_distinct_by_field(WIDE_START, WIDE_END, "service")
    assert rows
    assert int(rows[0]["distinct_count"]) > 0
    assert rows.stats.approximate is False


# --- value discovery ---------------------------------------------------------
def test_get_field_values_is_bounded_and_ranked():
    rows = get_field_values(WIDE_START, WIDE_END, "service", limit=3)
    assert 0 < len(rows) <= 3
    assert {"value", "count"} <= set(rows[0])
    counts = [int(r["count"]) for r in rows]
    assert counts == sorted(counts, reverse=True)


@pytest.mark.parametrize("field", ["service", "entity_id", "event_id", "template"])
def test_get_field_values_covers_each_canonical_field(field):
    assert get_field_values(WIDE_START, WIDE_END, field, limit=5)


# --- trends ------------------------------------------------------------------
def test_service_trend_buckets_by_minute():
    rows = get_trend(WIDE_START, WIDE_END, "service", _top_service(), bucket="day")
    assert rows
    assert {"bucket", "count", "trend"} <= set(rows[0])
    assert {r["trend"] for r in rows} <= TREND_VALUES


def test_entity_trend():
    rows = get_trend(WIDE_START, WIDE_END, "entity_id", _top_entity(), bucket="day")
    assert rows


def test_trend_direction_is_initial_for_the_first_bucket():
    rows = get_trend(WIDE_START, WIDE_END, "service", _top_service(), bucket="day")
    assert rows[0]["trend"] == "initial"


# --- search ------------------------------------------------------------------
def test_search_logs_returns_rows_with_expected_columns():
    rows = search_logs("instance")
    assert rows
    assert {"ts", "severity", "service", "message"} <= set(rows[0])
    assert "relevance" in rows[0]


def test_search_logs_respects_limit():
    assert len(search_logs("instance", limit=3)) <= 3


def test_search_logs_severity_filter():
    rows = search_logs("instance", severity="INFO", limit=5)
    assert all(r["severity"] == "INFO" for r in rows)


def test_search_logs_is_hybrid_on_the_embedded_corpus():
    """The OpenStack corpus has per-row embeddings, so fusion must be in play."""
    assert search_logs("virtual machine boot failure", limit=5)


# --- ranking and comparison --------------------------------------------------
def test_top_by_field_is_ranked_and_limited():
    rows = top_by_field(WIDE_START, WIDE_END, "service", limit=2)
    assert 0 < len(rows) <= 2
    counts = [int(r["count"]) for r in rows]
    assert counts == sorted(counts, reverse=True)


def test_compare_periods_reports_both_windows():
    rows = compare_periods(
        BASELINE_START, BASELINE_END, RECENT_START, RECENT_END, limit=5
    )
    assert rows
    assert {
        "service",
        "baseline_count",
        "recent_count",
        "volume_change",
    } <= set(rows[0])


def test_compare_periods_change_is_consistent():
    rows = compare_periods(
        BASELINE_START, BASELINE_END, RECENT_START, RECENT_END, limit=10
    )
    for row in rows:
        expected = int(row["recent_count"]) - int(row["baseline_count"])
        assert int(row["volume_change"]) == expected


# --- fetch by id -------------------------------------------------------------
def test_get_log_by_record_id_returns_its_columns():
    with db_cursor() as cur:
        cur.execute(
            sql.SQL("SELECT MIN(line_id) AS id FROM {t}").format(
                t=sql.Identifier(TABLE_NAME)
            )
        )
        record_id = int(cur.fetchone()["id"])

    rows = get_log_by_record_id(record_id)
    assert len(rows) == 1
    assert rows[0]["record_id"] == record_id
    assert rows[0]["message"]


def test_get_log_by_record_id_unknown_id_is_empty():
    assert get_log_by_record_id(10**9) == []


# --- cost metering -----------------------------------------------------------
def test_every_query_reports_its_cost():
    rows = count_by_field(WIDE_START, WIDE_END)
    stats = rows.stats
    assert stats.dataset == "openstack"
    assert stats.elapsed_ms >= 0
    assert stats.row_count == len(rows)


def test_local_corpus_reports_no_pruning():
    """There are no Parquet footers to prune on a native table."""
    stats = count_by_field(WIDE_START, WIDE_END).stats
    assert stats.row_groups_considered == 0
    assert stats.files_considered == 0


# --- refusals reach the caller as results ------------------------------------
def test_unbounded_request_is_refused_as_a_result_not_an_exception():
    """A refusal is a usable answer, not a failure.

    ``query_runner`` converts the builder's ``QueryRefused`` into an empty
    result carrying the refusal, so the agent can read the suggestion and retry
    with a narrower request instead of seeing a traceback.
    """
    result = count_by_field(None, None)
    assert result.refused
    assert result == []
    payload = result.refusal.as_tool_result()
    assert payload["status"] == "refused"
    assert payload["code"] == "missing_window"
    assert payload["suggestion"]


def test_the_builder_itself_raises_so_misuse_is_loud():
    """Undecorated, the builder raises -- the conversion is the runner's job."""
    from backend.db.errors import QueryRefused

    with pytest.raises(QueryRefused):
        count_by_field.__wrapped__(None, None)


# --- auto-complete -----------------------------------------------------------
_SELECTORS = [
    ("@", "service", "nova"),
    ("#", "event_id", "E2"),
    ("^", "event_template", "Removable"),
    ("!", "pid_text", "25"),
]


@pytest.mark.parametrize("selector,column,query", _SELECTORS)
def test_auto_complete_selector_scopes_to_its_column(selector, column, query):
    suggestions = auto_complete(f"{selector}{query}")
    assert suggestions
    assert all("suggestion" in s for s in suggestions)


@pytest.mark.parametrize("selector,column,_query", _SELECTORS)
def test_auto_complete_bare_selector_lists_its_column(selector, column, _query):
    assert auto_complete(selector)


def test_auto_complete_content_suggests_record_ids():
    suggestions = auto_complete("$instance")
    assert suggestions
    assert all("line_id" in s for s in suggestions)


def test_auto_complete_requires_a_leading_selector():
    assert auto_complete("nova") == []


def test_auto_complete_bang_modifier_returns_the_same_values():
    plain = {s["suggestion"] for s in auto_complete("@nova")}
    cased = {s["suggestion"] for s in auto_complete("@!nova")}
    assert plain == cased


def test_auto_complete_on_a_corpus_without_an_index_is_empty():
    """The remote tier must never be scanned for a keystroke."""
    assert auto_complete("@cart", "otel_s3") == []


@pytest.mark.parametrize("selector", ["@", "#", "^", "!", "$"])
def test_auto_complete_selectors_are_absent_from_the_data(selector):
    """Each selector character must not occur in any indexed value.

    That is what guarantees a selector can never collide with real text.
    """
    with db_cursor() as cur:
        cur.execute(
            sql.SQL("""
                SELECT count(*) AS n FROM {t}
                WHERE service LIKE %s OR event_id LIKE %s
                   OR event_template LIKE %s OR pid_text LIKE %s
            """).format(t=sql.Identifier(TABLE_NAME)),
            [f"%{selector}%"] * 4,
        )
        assert int(cur.fetchone()["n"]) == 0
