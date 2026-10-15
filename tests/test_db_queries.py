"""Integration tests for the SereneDB query layer.

These replace the manual ``if __name__ == "__main__"`` blocks that used to live
in the db query modules. They run against a live SereneDB on localhost:7890 and
fail (never skip) when it is unreachable, so a broken database setup is loud.
"""

import socket

import pytest

from backend.db import (
    auto_complete,
    compare_periods,
    count_by_category,
    count_event_id_by_category,
    count_pid_by_category,
    count_service_by_category,
    get_event_id_trend_by_category,
    get_event_ids,
    get_log_by_line_id,
    get_pid_trend_by_category,
    get_pids,
    get_service_trend_by_category,
    get_services,
    search_logs,
    top_in_category_by_service,
)
from backend.db.config import SERENEDB_PORT, TABLE_NAME
from backend.db.connection import db_cursor
from psycopg2 import sql

# The sample dataset (OpenStack_2k.log_structured.csv) spans this window.
START = "2017-05-16 00:00:00"
END = "2017-05-16 00:10:00"
BASELINE_START = "2017-05-16 00:00:00"
BASELINE_END = "2017-05-16 00:01:00"
RECENT_START = "2017-05-16 00:02:00"
RECENT_END = "2017-05-16 00:03:00"

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
        pids = get_pids()
    except Exception as e:  # noqa: BLE001 - surface any connection/query failure
        pytest.fail(
            f"SereneDB is reachable on localhost:{SERENEDB_PORT} but querying "
            f"failed: {e}"
        )

    if not pids:
        pytest.fail(
            "The 'log' table is empty. Seed it with "
            "`uv run python -m backend.db.setup` before running the DB tests."
        )


def _top_pid() -> int:
    return int(count_pid_by_category(START, END)[0]["pid"])


def _top_service() -> str:
    return top_in_category_by_service(START, END)[0]["service"]


# --- counts ------------------------------------------------------------------


def test_count_by_category_all():
    rows = count_by_category(START, END)
    assert rows
    assert all({"category", "count"} <= row.keys() for row in rows)
    assert sum(row["count"] for row in rows) > 0


def test_count_by_category_filtered_to_one_severity():
    rows = count_by_category(START, END, category="INFO")
    assert rows
    assert all(row["category"] == "INFO" for row in rows)


def test_count_by_category_filter_excludes_other_severities():
    rows = count_by_category(START, END, category="ERROR")
    assert all(row["category"] == "ERROR" for row in rows)


def test_count_pid_by_category():
    rows = count_pid_by_category(START, END)
    assert rows
    assert all({"pid", "category", "count"} <= row.keys() for row in rows)


def test_count_service_by_category():
    rows = count_service_by_category(START, END)
    assert rows
    assert all({"service", "category", "count"} <= row.keys() for row in rows)


def test_count_event_id_by_category():
    rows = count_event_id_by_category(START, END)
    assert rows
    assert all({"event_id", "category", "count"} <= row.keys() for row in rows)


# --- distinct listings -------------------------------------------------------


def test_get_pids():
    rows = get_pids()
    assert rows
    values = [row["pid"] for row in rows]
    assert len(values) == len(set(values))


def test_get_services():
    rows = get_services()
    assert rows
    values = [row["service"] for row in rows]
    assert len(values) == len(set(values))


def test_get_event_ids():
    rows = get_event_ids()
    assert rows
    values = [row["event_id"] for row in rows]
    assert len(values) == len(set(values))


# --- trends ------------------------------------------------------------------


def _assert_trend_rows(rows):
    assert rows
    assert all({"minute", "count", "prev_count", "trend"} <= row.keys() for row in rows)
    assert {row["trend"] for row in rows} <= TREND_VALUES


def test_pid_trend():
    _assert_trend_rows(get_pid_trend_by_category(START, END, _top_pid()))


def test_service_trend():
    _assert_trend_rows(get_service_trend_by_category(START, END, _top_service()))


def test_event_id_trend():
    event_id = count_event_id_by_category(START, END)[0]["event_id"]
    _assert_trend_rows(get_event_id_trend_by_category(START, END, event_id))


# --- search ------------------------------------------------------------------


def test_search_logs_returns_rows_with_expected_columns():
    rows = search_logs("network")
    assert rows
    expected = {"log_timestamp", "pid", "category", "service", "content", "relevance"}
    assert all(expected <= row.keys() for row in rows)


def test_search_logs_respects_limit():
    rows = search_logs("network", limit=5)
    assert len(rows) <= 5


def test_search_logs_category_filter():
    rows = search_logs("network", category="WARNING")
    assert all(row["category"] == "WARNING" for row in rows)


# --- aggregation / comparison ------------------------------------------------


def test_top_in_category_by_service_is_ranked_and_limited():
    rows = top_in_category_by_service(START, END, limit=3)
    assert rows
    assert len(rows) <= 3
    assert all({"service", "count"} <= row.keys() for row in rows)
    counts = [row["count"] for row in rows]
    assert counts == sorted(counts, reverse=True)


def test_compare_periods():
    rows = compare_periods(
        BASELINE_START, BASELINE_END, RECENT_START, RECENT_END, limit=5
    )
    assert rows
    assert len(rows) <= 5
    expected = {
        "service",
        "category",
        "baseline_count",
        "recent_count",
        "volume_change",
        "percentage_change",
    }
    assert all(expected <= row.keys() for row in rows)


# --- auto-complete -----------------------------------------------------------

# A selector scoping the lookup to one column, with a query that matches it.
# ``pid`` is suggested through its text mirror ``pid_text``; ``content`` matches
# whole lines rather than single words.
SELECTOR_CASES = [
    ("@", "service", "nova"),
    ("#", "event_id", "E25"),
    ("^", "event_template", "Removable"),
    ("!", "pid_text", "25"),
    ("$", "content", "server"),
]


def _column_values(column: str) -> set[str]:
    """Every distinct value of ``column`` in the log table."""
    with db_cursor() as cur:
        cur.execute(
            sql.SQL("SELECT DISTINCT {col} FROM {table}").format(
                col=sql.Identifier(column), table=sql.Identifier(TABLE_NAME)
            )
        )
        return {row[column] for row in cur.fetchall()}


@pytest.mark.parametrize("selector,column,query", SELECTOR_CASES)
def test_auto_complete_selector_scopes_to_its_column(selector, column, query):
    rows = auto_complete(f"{selector}{query}")
    assert rows
    # Every suggestion comes from the scoped column and nowhere else.
    assert {row["suggestion"] for row in rows} <= _column_values(column)


@pytest.mark.parametrize("selector,column,_query", SELECTOR_CASES)
def test_auto_complete_bare_selector_lists_its_column(selector, column, _query):
    rows = auto_complete(selector)
    assert rows
    assert {row["suggestion"] for row in rows} <= _column_values(column)


def test_auto_complete_content_suggests_line_ids():
    # ``$`` content suggestions carry a ``line_id`` (shown and inserted as a
    # "Line ID: n" reference) instead of a result count.
    rows = auto_complete("$server")
    assert rows
    assert all(row.get("line_id") is not None for row in rows)

    with db_cursor() as cur:
        for row in rows:
            assert "count" not in row
            cur.execute(
                sql.SQL("SELECT content FROM {table} WHERE line_id = %s").format(
                    table=sql.Identifier(TABLE_NAME)
                ),
                (row["line_id"],),
            )
            assert cur.fetchone()["content"] == row["suggestion"]


def test_auto_complete_requires_a_leading_selector():
    # A selector only counts at the start: ``E25`` is an event_id, so a leading
    # ``@`` scopes to service and finds nothing, and a bare/trailing selector
    # leaves the query unscoped, so nothing is suggested.
    assert auto_complete("@E25") == []
    assert auto_complete("E25") == []
    assert auto_complete("E25@") == []


# --- get log by line_id ------------------------------------------------------

def test_get_log_by_line_id_returns_its_columns():
    rows = get_log_by_line_id(1)
    assert len(rows) == 1
    record = rows[0]
    assert record["line_id"] == 1
    # The derived pid_text mirror, the split log_date/log_time and the embedding
    # vector are deliberately not returned.
    assert set(record) == {
        "line_id",
        "log_record",
        "log_timestamp",
        "pid",
        "category",
        "service",
        "addr",
        "content",
        "event_id",
        "event_template",
    }


def test_get_log_by_line_id_unknown_id_is_empty():
    assert get_log_by_line_id(10_000_000) == []


@pytest.mark.parametrize("selector", [case[0] for case in SELECTOR_CASES])
def test_auto_complete_selectors_are_absent_from_the_data(selector):
    # A selector is only safe to strip because no indexed value contains it.
    with db_cursor() as cur:
        cur.execute(
            sql.SQL(
                "SELECT 1 FROM {table} "
                "WHERE position(%s in service) > 0 "
                "OR position(%s in event_id) > 0 "
                "OR position(%s in event_template) > 0 "
                "OR position(%s in pid_text) > 0 "
                "OR position(%s in content) > 0 LIMIT 1"
            ).format(table=sql.Identifier(TABLE_NAME)),
            (selector, selector, selector, selector, selector),
        )
        assert cur.fetchone() is None, f"{selector!r} occurs in the indexed values"
