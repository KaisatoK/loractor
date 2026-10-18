"""Profile OTel query latency with SereneDB ``EXPLAIN ANALYZE``.

This is an operator-facing profiler, not an application query path.  It builds
cases from the existing OTel query builders, executes each one as
``EXPLAIN ANALYZE <query>``, and reports SereneDB's server-side ``Total Time``.
The profiler never builds the in-place ``otel_slice`` index; that tier is only
profiled when an existing index already covers the selected window.

Run with:

    bash ./profile_otel.sh
    bash ./profile_otel.sh --runs 5 --window-seconds 60
"""

from __future__ import annotations

import argparse
import re
import statistics
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from psycopg2 import sql

from backend.db import external
from backend.db.connection import close_pool, db_cursor
from backend.db.count_query import (
    compare_periods,
    count_by_field,
    top_by_field,
)
from backend.db.datasets import resolve
from backend.db.errors import QueryRefused
from backend.db.pruning import load_row_groups
from backend.db.query import count_search_matches, search_logs
from backend.db.stats import QueryResult
from backend.db.trace_query import trace_logs
from backend.db.trend_query import get_trend
from backend.db.list_query import get_field_values
from backend.db.windows import build_window
from backend.db.config import SERENEDB_BULK_TIMEOUT_MS

TOTAL_TIME_RE = re.compile(r"\bTotal\s+Time\s*:\s*([0-9]+(?:\.[0-9]+)?)\s*s\b", re.I)
DEFAULT_RUNS = 5
DEFAULT_WINDOW_SECONDS = 10
DEFAULT_SEARCH = "error"


@dataclass(frozen=True)
class Latency:
    """Server-side timings in milliseconds for one profiled statement."""

    average_ms: float | None
    median_ms: float | None
    min_ms: float | None
    max_ms: float | None
    total_ms: float | None
    samples: tuple[float, ...]


@dataclass(frozen=True)
class ProfileCase:
    """One raw query builder invocation to profile."""

    label: str
    dataset: str
    builder: Callable[..., Any]
    kwargs: dict[str, Any]


@dataclass(frozen=True)
class ProfileRow:
    """A printable result, including refused and skipped cases."""

    label: str
    dataset: str
    status: str
    latency: Latency | None = None
    reason: str = ""


@dataclass(frozen=True)
class Discovery:
    """Values needed by query shapes whose arguments are data-dependent."""

    service: str | None
    trace_id: str | None


def _row_text(rows: Iterable[Any]) -> str:
    """Flatten psycopg2 rows into text without depending on column names.

    SereneDB's text EXPLAIN result is returned as one or more text cells.  The
    driver/cursor shape has varied between compatible clients, so accepting
    mappings, tuples and scalar rows keeps parsing independent of that detail.
    """
    values: list[str] = []
    for row in rows:
        if isinstance(row, Mapping):
            values.extend(str(value) for value in row.values())
        elif isinstance(row, (tuple, list)):
            values.extend(str(value) for value in row)
        else:
            values.append(str(row))
    return "\n".join(values)


def parse_total_seconds(output: str | Iterable[Any]) -> float | None:
    """Return SereneDB's ``Total Time`` in seconds, or ``None`` if absent."""
    text = output if isinstance(output, str) else _row_text(output)
    match = TOTAL_TIME_RE.search(text)
    return float(match.group(1)) if match else None


def explain_statement(query: sql.Composable) -> sql.Composable:
    """Prefix a builder-produced statement with SereneDB's analyzer."""
    return sql.SQL("EXPLAIN ANALYZE ") + query


def explain_latency(
    query: sql.Composable,
    params: Sequence[Any] | None = None,
    *,
    runs: int = DEFAULT_RUNS,
) -> Latency:
    """Execute one statement repeatedly and return parsed server timings.

    The average, median, minimum, maximum, and total are calculated from all
    successfully parsed samples. A missing ``Total Time`` is not replaced with
    client wall-clock time because that would mix two different measurements.
    """
    if runs < 1:
        raise ValueError("runs must be at least 1")

    samples: list[float] = []
    bound = tuple(params or ())
    for _ in range(runs):
        with db_cursor() as cur:
            cur.execute(
                sql.SQL("SET LOCAL statement_timeout = {ms}").format(
                    ms=sql.Literal(SERENEDB_BULK_TIMEOUT_MS)
                )
            )
            cur.execute(explain_statement(query), bound)
            seconds = parse_total_seconds(cur.fetchall())
        milliseconds = seconds * 1000 if seconds is not None else None
        if milliseconds is not None:
            samples.append(milliseconds)

    return Latency(
        average_ms=statistics.fmean(samples) if samples else None,
        median_ms=statistics.median(samples) if samples else None,
        min_ms=min(samples) if samples else None,
        max_ms=max(samples) if samples else None,
        total_ms=sum(samples) if samples else None,
        samples=tuple(samples),
    )


def _timestamp(value: datetime) -> str:
    """Format a footer timestamp in the driver's safe microsecond precision."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    value = value.astimezone(timezone.utc)
    return value.isoformat(sep=" ", timespec="microseconds")


def default_window(start: str | None, seconds: int) -> tuple[str, str]:
    """Choose a bounded OTel window from the first Parquet footer."""
    dataset = resolve("otel_s3")
    if start:
        start_text = start
        # Validate the explicitly supplied start using the same dataset rules.
        datetime.fromisoformat(start.replace("Z", "+00:00"))
    else:
        groups = load_row_groups(dataset.sources[0], dataset.field("ts").column or "Timestamp")
        if not groups:
            raise RuntimeError("no Parquet footer row groups found for otel_s3")
        start_text = _timestamp(groups[0].ts_min)
    parsed = datetime.fromisoformat(start_text.replace("Z", "+00:00"))
    end_text = _timestamp(parsed + timedelta(seconds=seconds))
    # This also enforces SereneDB's OTel maximum window before any query runs.
    build_window(dataset, start_text, end_text)
    return start_text, end_text


def discover_values(start: str, end: str) -> Discovery:
    """Find one service and trace id for the data-dependent query cases."""
    service: str | None = None
    result: QueryResult = get_field_values(
        start, end, field="service", dataset="otel_s3", limit=1
    )
    if result and result[0].get("value") is not None:
        service = str(result[0]["value"])

    dataset = resolve("otel_s3")
    ts = dataset.sql_for("ts")
    entity = dataset.sql_for("entity_id")
    with db_cursor() as cur:
        cur.execute(
            sql.SQL(
                "SELECT {entity} AS trace_id FROM {relation} "
                "WHERE {ts} >= %s::timestamptz AND {ts} < %s::timestamptz "
                "AND {entity} IS NOT NULL LIMIT 1"
            ).format(entity=entity, relation=dataset.scan_relation(), ts=ts),
            (start, end),
        )
        row = cur.fetchone()
    trace_id = str(row["trace_id"]) if row and row.get("trace_id") else None
    return Discovery(service=service, trace_id=trace_id)


def build_cases(
    start: str,
    end: str,
    *,
    search: str = DEFAULT_SEARCH,
    discovery: Discovery | None = None,
) -> list[ProfileCase]:
    """Build all OTel query shapes from the existing query builders."""
    values = discovery or discover_values(start, end)
    cases: list[ProfileCase] = [
        ProfileCase(
            "count_by_field", "otel_s3", count_by_field,
            {"start_time": start, "end_time": end, "group_by": "service", "dataset": "otel_s3", "limit": 10},
        ),
        ProfileCase(
            "top_by_field", "otel_s3", top_by_field,
            {"start_time": start, "end_time": end, "field": "service", "dataset": "otel_s3", "limit": 5},
        ),
        ProfileCase(
            "compare_periods", "otel_s3", compare_periods,
            {
                "baseline_start": start,
                "baseline_end": _timestamp(
                    datetime.fromisoformat(end.replace("Z", "+00:00"))
                    - (datetime.fromisoformat(end.replace("Z", "+00:00")) - datetime.fromisoformat(start.replace("Z", "+00:00"))) / 2
                ),
                "recent_start": _timestamp(
                    datetime.fromisoformat(end.replace("Z", "+00:00"))
                    - (datetime.fromisoformat(end.replace("Z", "+00:00")) - datetime.fromisoformat(start.replace("Z", "+00:00"))) / 2
                ),
                "recent_end": end,
                "dataset": "otel_s3",
                "field": "service",
                "limit": 5,
            },
        ),
        ProfileCase(
            "get_field_values", "otel_s3", get_field_values,
            {"start_time": start, "end_time": end, "field": "service", "dataset": "otel_s3", "limit": 10},
        ),
    ]
    if values.service:
        cases.append(
            ProfileCase(
                "get_trend", "otel_s3", get_trend,
                {
                    "start_time": start, "end_time": end, "field": "service",
                    "value": values.service, "dataset": "otel_s3", "bucket": "second",
                },
            )
        )
    if values.trace_id:
        cases.append(
            ProfileCase(
                "trace_logs", "otel_s3", trace_logs,
                {
                    "trace_id": values.trace_id, "start_time": start,
                    "end_time": end, "dataset": "otel_s3", "limit": 100,
                },
            )
        )
    return cases + [
        ProfileCase(
            "search_logs", "otel_slice", search_logs,
            {"query_text": search, "dataset": "otel_slice", "severity": "error", "limit": 10, "start_time": start, "end_time": end},
        ),
        ProfileCase(
            "count_search_matches", "otel_slice", count_search_matches,
            {"query_text": search, "dataset": "otel_slice", "severity": "error", "start_time": start, "end_time": end},
        ),
    ]


def _raw_query(case: ProfileCase) -> tuple[sql.Composable, Sequence[Any]]:
    """Build SQL without executing it, preserving builder refusal semantics."""
    builder = getattr(case.builder, "__wrapped__", case.builder)
    built = builder(**case.kwargs)
    return built[0], built[1]


def profile_cases(
    cases: Iterable[ProfileCase],
    *,
    runs: int,
    coverage: dict[str, Any] | None = None,
) -> list[ProfileRow]:
    """Profile cases, converting refusals and unavailable slice coverage to rows."""
    rows: list[ProfileRow] = []
    for case in cases:
        if case.dataset == "otel_slice" and not (coverage or {}).get("covers", False):
            covered = coverage or {}
            window = ""
            if covered.get("window_start") and covered.get("window_end"):
                window = f"; existing coverage {covered['window_start']}..{covered['window_end']}"
            rows.append(ProfileRow(case.label, case.dataset, "skipped", reason="no index" + window))
            continue
        try:
            query, params = _raw_query(case)
            latency = explain_latency(query, params, runs=runs)
        except QueryRefused as exc:
            refusal = exc.refusal
            rows.append(ProfileRow(case.label, case.dataset, "refused", reason=f"{refusal.reason} Suggestion: {refusal.suggestion}"))
        except (LookupError, RuntimeError) as exc:
            rows.append(ProfileRow(case.label, case.dataset, "skipped", reason=str(exc)))
        else:
            rows.append(ProfileRow(case.label, case.dataset, "ok", latency=latency))
    return rows


def _format_ms(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.2f}"


def _markdown_cell(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", "<br>")


def render_markdown(
    *,
    start: str,
    end: str,
    runs: int,
    search: str,
    coverage: dict[str, Any],
    rows: Iterable[ProfileRow],
) -> str:
    """Render the profiling result as a Markdown report."""
    generated = datetime.now(timezone.utc).isoformat(timespec="seconds")
    report_rows = list(rows)
    lines = [
        "# OTel EXPLAIN ANALYZE latency report",
        "",
        f"Generated: `{generated}`",
        "",
        "This report records SereneDB server-side `EXPLAIN ANALYZE` timings in "
        "milliseconds. It profiles the database functions behind the OTel tools, "
        "not the tool wrappers or client/network time.",
        "",
        f"- Window: `{start}` to `{end}`",
        f"- Repetitions per query: `{runs}`",
        f"- Search term for slice cases: `{search}`",
        f"- Existing `otel_slice` index covers the window: `{bool(coverage.get('covers', False))}`",
        "",
        "`avg ms`, `median ms`, `min ms`, and `max ms` are calculated from the "
        "successfully parsed runs. `total ms` is their cumulative server time. "
        "Skipped and refused cases do not execute an analyzed query.",
        "",
        "| DB function | Dataset | Status | Avg ms | Median ms | Min ms | Max ms | Total ms | Runs | Note |",
        "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for row in report_rows:
        if row.latency:
            values = (
                _format_ms(row.latency.average_ms),
                _format_ms(row.latency.median_ms),
                _format_ms(row.latency.min_ms),
                _format_ms(row.latency.max_ms),
                _format_ms(row.latency.total_ms),
                str(len(row.latency.samples)),
                "",
            )
        else:
            values = ("n/a", "n/a", "n/a", "n/a", "n/a", "-", row.reason)
        lines.append(
            "| " + " | ".join(
                _markdown_cell(value)
                for value in (row.label, row.dataset, row.status, *values)
            ) + " |"
        )
    return "\n".join(lines) + "\n"


def write_markdown_report(path: str | Path, content: str) -> Path:
    """Write a report, creating its parent directory when necessary."""
    report_path = Path(path)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(content, encoding="utf-8")
    return report_path


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="bash ./profile_otel.sh",
        description=__doc__,
    )
    parser.add_argument("--runs", type=int, default=DEFAULT_RUNS, help="EXPLAIN ANALYZE repetitions per query (default: 5)")
    parser.add_argument("--start", help="UTC window start; defaults to the first OTel footer timestamp")
    parser.add_argument("--window-seconds", type=int, default=DEFAULT_WINDOW_SECONDS, help="bounded OTel profiling window (default: 10)")
    parser.add_argument("--search", default=DEFAULT_SEARCH, help="text used for the existing otel_slice search cases")
    parser.add_argument("--only", help="comma-separated case labels to run")
    parser.add_argument(
        "--output",
        default="docs/otel_profile.md",
        help="Markdown report path (default: docs/otel_profile.md)",
    )
    args = parser.parse_args(argv)
    if args.runs < 1:
        parser.error("--runs must be at least 1")
    if args.window_seconds < 1:
        parser.error("--window-seconds must be at least 1")

    start, end = default_window(args.start, args.window_seconds)
    selected = {label.strip() for label in args.only.split(",")} if args.only else None
    all_labels = {case.label for case in build_cases(start, end, search=args.search, discovery=Discovery("service", "trace"))}
    if selected and not selected <= all_labels:
        parser.error("unknown --only label(s): " + ", ".join(sorted(selected - all_labels)))

    cases = build_cases(start, end, search=args.search)
    if selected:
        cases = [case for case in cases if case.label in selected]
    slice_coverage = external.index_coverage(start, end)
    rows = profile_cases(cases, runs=args.runs, coverage=slice_coverage)
    report = render_markdown(
        start=start,
        end=end,
        runs=args.runs,
        search=args.search,
        coverage=slice_coverage,
        rows=rows,
    )
    write_markdown_report(args.output, report)
    close_pool()


if __name__ == "__main__":
    main()
