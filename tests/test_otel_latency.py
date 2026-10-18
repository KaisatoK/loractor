"""Offline tests for the OTel EXPLAIN ANALYZE latency profiler."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from psycopg2 import sql

from backend.db import otel_latency as profiler
from backend.db.errors import QueryRefused, refuse


class FakeCursor:
    def __init__(self, outputs):
        self.outputs = iter(outputs)
        self.executed = []

    def execute(self, statement, params=None):
        self.executed.append((statement, params))

    def fetchall(self):
        return next(self.outputs)


class FakeCursorContext:
    def __init__(self, cursor):
        self.cursor = cursor

    def __enter__(self):
        return self.cursor

    def __exit__(self, *args):
        return False


def test_parse_total_time_from_documented_text_and_rows():
    assert profiler.parse_total_seconds("Total Time: 0.0029s") == pytest.approx(0.0029)
    assert profiler.parse_total_seconds([
        {"explain_value": "Query Profiling Information"},
        {"explain_value": "Total Time: 0.0085s"},
    ]) == pytest.approx(0.0085)
    assert profiler.parse_total_seconds("no profiling result") is None


def test_explain_latency_executes_explain_and_returns_average_and_total(monkeypatch):
    cursor = FakeCursor([
        [{"explain_value": "Total Time: 0.010s"}],
        [{"explain_value": "Total Time: 0.004s"}],
        [{"explain_value": "Total Time: 0.006s"}],
    ])
    monkeypatch.setattr(profiler, "db_cursor", lambda: FakeCursorContext(cursor))

    result = profiler.explain_latency(sql.SQL("SELECT 1"), ["kept"], runs=3)

    assert result.average_ms == pytest.approx(20.0 / 3)
    assert result.median_ms == pytest.approx(6.0)
    assert result.min_ms == pytest.approx(4.0)
    assert result.max_ms == pytest.approx(10.0)
    assert result.total_ms == pytest.approx(20.0)
    assert result.samples == pytest.approx((10.0, 4.0, 6.0))
    assert profiler.DEFAULT_RUNS == 5
    assert len(cursor.executed) == 6
    explain_statement, params = cursor.executed[1]
    assert isinstance(explain_statement, sql.Composed)
    assert params == ("kept",)


def test_build_cases_contains_dynamic_cases_without_executing_discovery():
    cases = profiler.build_cases(
        "2025-09-23 00:00:00+00",
        "2025-09-23 00:00:10+00",
        discovery=profiler.Discovery("checkout", "trace-1"),
    )
    labels = {case.label for case in cases}
    assert {
        "count_by_field", "top_by_field", "compare_periods",
        "get_field_values", "get_trend", "trace_logs", "search_logs",
        "count_search_matches",
    } <= labels
    trend = next(case for case in cases if case.label == "get_trend")
    assert trend.kwargs["value"] == "checkout"


def test_slice_cases_are_skipped_without_coverage(monkeypatch):
    calls = []
    monkeypatch.setattr(profiler, "explain_latency", lambda *args, **kwargs: calls.append(args))
    cases = [case for case in profiler.build_cases(
        "2025-09-23 00:00:00+00", "2025-09-23 00:00:10+00",
        discovery=profiler.Discovery(None, None),
    ) if case.dataset == "otel_slice"]

    rows = profiler.profile_cases(cases, runs=1, coverage={"covers": False})

    assert [row.status for row in rows] == ["skipped", "skipped"]
    assert not calls
    assert all("no index" in row.reason for row in rows)


def test_refused_case_is_reported_not_raised(monkeypatch):
    def refused(**kwargs):
        raise refuse("too wide", "narrow it", code="window_too_wide")

    case = profiler.ProfileCase("bad", "otel_s3", refused, {})
    rows = profiler.profile_cases([case], runs=1, coverage={"covers": True})
    assert rows[0].status == "refused"
    assert "too wide" in rows[0].reason
    assert "narrow it" in rows[0].reason


def test_render_markdown_contains_metrics_and_no_terminal_table():
    latency = profiler.Latency(
        average_ms=2.0,
        median_ms=1.5,
        min_ms=1.0,
        max_ms=3.0,
        total_ms=6.0,
        samples=(1.0, 3.0, 2.0),
    )
    report = profiler.render_markdown(
        start="2025-09-23 00:00:00+00",
        end="2025-09-23 00:00:10+00",
        runs=3,
        search="error",
        coverage={"covers": False},
        rows=[profiler.ProfileRow("count_by_field", "otel_s3", "ok", latency)],
    )
    assert "| DB function | Dataset | Status | Avg ms | Median ms | Min ms | Max ms | Total ms | Runs | Note |" in report
    assert "| count_by_field | otel_s3 | ok | 2.00 | 1.50 | 1.00 | 3.00 | 6.00 | 3 |  |" in report
    assert "2025-09-23 00:00:00+00" in report
