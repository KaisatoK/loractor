"""Counting queries, built from canonical fields.

The original four near-identical functions (``count_by_category``,
``count_pid_by_category``, ``count_service_by_category``,
``count_event_id_by_category``) differed only in which column they grouped by.
They are one function here, parameterized by a canonical field name, which is
why a new corpus needs no new counting code.

Every count is bounded: a mandatory time window, a pre-flight pruning estimate
for remote corpora, and a ``LIMIT`` on the number of groups returned. The
original versions had none of those and were safe only because the table held
2,000 rows.
"""

from __future__ import annotations

import logging

from psycopg2 import sql

from backend.db import predicates as pred
from backend.db.connection import bulk_query
from backend.db.datasets import Dataset, DatasetKind, resolve
from backend.db.pruning import plan_and_guard
from backend.db.stats import QueryStats
from backend.db.windows import build_window

logger = logging.getLogger(__name__)


def _prepare(
    dataset_id: str | None, start_time: str, end_time: str
) -> tuple[Dataset, object, QueryStats]:
    """Resolve the dataset, validate the window, and cost it before running."""
    dataset = resolve(dataset_id)
    window = build_window(dataset, start_time, end_time)
    stats = (
        plan_and_guard(dataset, window)
        if dataset.kind is DatasetKind.REMOTE_PARQUET
        else QueryStats(
            dataset=dataset.id,
            approximate=not dataset.exact_counts,
            window_seconds=window.seconds,
        )
    )
    return dataset, window, stats


@bulk_query
def count_by_field(
    start_time: str,
    end_time: str,
    group_by: str | None = None,
    dataset: str | None = None,
    severity: str | None = None,
    limit: int | None = None,
) -> tuple[sql.Composable, list, QueryStats]:
    """Count rows per severity, optionally broken down by another field.

    ``group_by`` is a canonical field name (``service``, ``entity_id``,
    ``event_id``) or ``None`` for the severity-only breakdown that the original
    ``count_by_category`` produced.
    """
    ds, window, stats = _prepare(dataset, start_time, end_time)
    logger.debug(
        "Counting %s on %s by %s from %s to %s",
        severity or "all severities",
        ds.id,
        group_by or "severity",
        start_time,
        end_time,
    )

    group_columns: list[sql.Composable] = []
    select_columns: list[sql.Composable] = []

    if group_by:
        column = ds.sql_for(group_by)
        group_columns.append(column)
        select_columns.append(
            sql.SQL("{col} AS {alias}").format(
                col=column, alias=sql.Identifier(group_by)
            )
        )

    if ds.has_field("severity"):
        severity_column = ds.sql_for("severity")
        group_columns.append(severity_column)
        select_columns.append(
            sql.SQL("{col} AS severity").format(col=severity_column)
        )

    clause, params = pred.where(
        pred.ts_filter(ds, window), pred.severity_filter(ds, severity)
    )

    query = sql.SQL("""
        SELECT {select}, count(*) AS count
        FROM {relation}
        {clause}
        GROUP BY {group}
        ORDER BY count DESC
        {limit}
    """).format(
        select=sql.SQL(", ").join(select_columns),
        relation=ds.scan_relation(),
        clause=clause,
        group=sql.SQL(", ").join(group_columns),
        limit=pred.bounded_limit(ds, limit),
    )
    return query, params, stats


@bulk_query
def count_distinct_by_field(
    start_time: str,
    end_time: str,
    field: str,
    dataset: str | None = None,
    severity: str | None = None,
) -> tuple[sql.Composable, list, QueryStats]:
    """How many distinct values a field takes in the window.

    Uses ``approx_count_distinct`` on corpora that do not promise exact counts,
    because the exact version builds a hash table over every value in the
    window -- the aggregate that most needs approximating at this scale.
    """
    ds, window, stats = _prepare(dataset, start_time, end_time)
    clause, params = pred.where(
        pred.ts_filter(ds, window), pred.severity_filter(ds, severity)
    )

    query = sql.SQL("""
        SELECT {agg} AS distinct_count, count(*) AS total_rows
        FROM {relation}
        {clause}
    """).format(
        agg=pred.approx_distinct(ds, field),
        relation=ds.scan_relation(),
        clause=clause,
    )
    stats.approximate = stats.approximate or not ds.exact_counts
    if not ds.exact_counts:
        stats.notes.append(f"distinct count of {field} is approximate")
    return query, params, stats


@bulk_query
def top_by_field(
    start_time: str,
    end_time: str,
    field: str = "service",
    dataset: str | None = None,
    severity: str | None = None,
    limit: int = 5,
) -> tuple[sql.Composable, list, QueryStats]:
    """Rank a field's values by row volume in the window.

    Replaces ``top_in_category_by_service``, generalized off ``service``.
    """
    ds, window, stats = _prepare(dataset, start_time, end_time)
    clause, params = pred.where(
        pred.ts_filter(ds, window), pred.severity_filter(ds, severity)
    )

    query = sql.SQL("""
        SELECT {col} AS {alias}, count(*) AS count
        FROM {relation}
        {clause}
        GROUP BY {col}
        ORDER BY count DESC
        {limit}
    """).format(
        col=ds.sql_for(field),
        alias=sql.Identifier(field),
        relation=ds.scan_relation(),
        clause=clause,
        limit=pred.bounded_limit(ds, limit),
    )
    return query, params, stats


@bulk_query
def compare_periods(
    baseline_start: str,
    baseline_end: str,
    recent_start: str,
    recent_end: str,
    dataset: str | None = None,
    field: str = "service",
    limit: int = 5,
) -> tuple[sql.Composable, list, QueryStats]:
    """Compare per-field, per-severity counts between two windows.

    Both windows are validated and costed; the reported estimate is their sum,
    because both are scanned.
    """
    ds = resolve(dataset)
    baseline = build_window(ds, baseline_start, baseline_end)
    recent = build_window(ds, recent_start, recent_end)

    stats = QueryStats(dataset=ds.id, approximate=not ds.exact_counts)
    if ds.kind is DatasetKind.REMOTE_PARQUET:
        first = plan_and_guard(ds, baseline)
        second = plan_and_guard(ds, recent)
        stats = first
        stats.files_pruned += second.files_pruned
        stats.row_groups_read += second.row_groups_read
        stats.row_groups_considered += second.row_groups_considered
        stats.est_rows_scanned += second.est_rows_scanned
        stats.est_bytes_read += second.est_bytes_read
    stats.window_seconds = baseline.seconds + recent.seconds

    group_field = ds.sql_for(field)
    has_severity = ds.has_field("severity")
    severity_column = ds.sql_for("severity") if has_severity else None

    def period(window) -> tuple[sql.Composable, list]:
        clause, params = pred.where(pred.ts_filter(ds, window))
        columns = [sql.SQL("{col} AS grp").format(col=group_field)]
        group = [group_field]
        if severity_column is not None:
            columns.append(sql.SQL("{col} AS sev").format(col=severity_column))
            group.append(severity_column)
        return (
            sql.SQL("""
                SELECT {columns}, count(*) AS n
                FROM {relation} {clause}
                GROUP BY {group}
            """).format(
                columns=sql.SQL(", ").join(columns),
                relation=ds.scan_relation(),
                clause=clause,
                group=sql.SQL(", ").join(group),
            ),
            params,
        )

    baseline_sql, baseline_params = period(baseline)
    recent_sql, recent_params = period(recent)

    join_keys = [sql.SQL("r.grp = b.grp")]
    select_sev: sql.Composable = sql.SQL("")
    if severity_column is not None:
        join_keys.append(sql.SQL("r.sev = b.sev"))
        select_sev = sql.SQL("COALESCE(r.sev, b.sev) AS severity,")

    query = sql.SQL("""
        WITH baseline AS ({baseline}), recent AS ({recent})
        SELECT
            COALESCE(r.grp, b.grp) AS {alias},
            {select_sev}
            COALESCE(b.n, 0) AS baseline_count,
            COALESCE(r.n, 0) AS recent_count,
            COALESCE(r.n, 0) - COALESCE(b.n, 0) AS volume_change,
            ROUND(
                (COALESCE(r.n, 0) - COALESCE(b.n, 0)) * 100.0
                / NULLIF(b.n, 0), 2
            ) AS percentage_change
        FROM recent r
        FULL OUTER JOIN baseline b ON {join}
        ORDER BY volume_change DESC
        {limit}
    """).format(
        baseline=baseline_sql,
        recent=recent_sql,
        alias=sql.Identifier(field),
        select_sev=select_sev,
        join=sql.SQL(" AND ").join(join_keys),
        limit=pred.bounded_limit(ds, limit),
    )
    return query, baseline_params + recent_params, stats
