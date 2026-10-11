"""Integration tests for the SereneDB query layer.

These replace the manual ``if __name__ == "__main__"`` blocks that used to live
in the db query modules. They run against a live SereneDB on localhost:7890 and
fail (never skip) when it is unreachable, so a broken database setup is loud.
"""

import socket

import pytest

from backend.db import (
    compare_periods,
    count_by_category,
    count_event_id_by_category,
    count_pid_by_category,
    count_service_by_category,
    get_event_id_trend_by_category,
    get_event_ids,
    get_pid_trend_by_category,
    get_pids,
    get_service_trend_by_category,
    get_services,
    search_logs,
    top_in_category_by_service,
)
from backend.db.config import SERENEDB_PORT

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
