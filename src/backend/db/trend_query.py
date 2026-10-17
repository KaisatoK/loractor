"""Trend queries -- how one entity's volume moves over time.

The original three functions bucketed on ``EXTRACT(MINUTE FROM log_time)``,
which has two problems: it folds every hour onto the same sixty buckets, so the
series is only meaningful inside a single hour, and ``log_time`` is an
OpenStack-only column that the OTel corpus does not have.

Both are fixed by ``date_trunc`` on the canonical timestamp with an explicit
bucket, so a trend can span seconds or days and reads the same on every corpus.

**Every bucket in the window is returned, including empty ones.** A plain
``GROUP BY`` only yields buckets that have rows, so a quiet hour vanished: the
series then showed a gap as a flat line, and ``LAG`` compared buckets that were
not adjacent, calling a drop to zero "flat" or a return from silence "up" from
the wrong predecessor. The buckets are now generated with ``generate_series``
and the counts left-joined onto them, with 0 where nothing was logged.
"""

from __future__ import annotations

import logging

from psycopg2 import sql

from backend.db import predicates as pred
from backend.db.connection import bulk_query
from backend.db.datasets import Dataset, DatasetKind, resolve
from backend.db.errors import refuse
from backend.db.pruning import plan_and_guard
from backend.db.stats import QueryStats
from backend.db.windows import build_window

logger = logging.getLogger(__name__)

# Rows per trend. More than this and the series is noise rather than a trend,
# and the result stops fitting in a tool result the model can read. With
# ``group_by="severity"`` it bounds buckets x severity levels, the row count.
MAX_BUCKETS = 240

# Rows a trend aims for when the caller names no bucket. Kept at the 100 rows
# a result record keeps for the answer and the priority score
# (``tools.results.MAX_RESULT_ROWS``), so a default trend is never cut short.
DEFAULT_MAX_POINTS = 100

# Nominal seconds per bucket, used only to predict the result size. Months vary
# in length, so the figure is an average -- close enough to decide whether a
# query would return hundreds of rows.
BUCKET_SECONDS = {
    "second": 1,
    "minute": 60,
    "hour": 3_600,
    "day": 86_400,
    "week": 604_800,
    "month": 2_629_746,
}

# What to suggest when a window needs too many buckets. Terminates at month,
# where the only remaining advice is to narrow the window.
COARSER_BUCKET = {
    "second": "minute",
    "minute": "hour",
    "hour": "day",
    "day": "week",
    "week": "month",
}

GROUP_BY = ("severity",)


def _series_count(dataset: Dataset, group_by: str | None) -> int:
    """Upper bound on the series a trend returns: one per declared severity."""
    return max(len(dataset.severity_values), 1) if group_by else 1


def _points(window_seconds: float, bucket: str, series: int) -> float:
    return window_seconds / BUCKET_SECONDS[bucket] * series


def choose_bucket(window_seconds: float, series: int = 1) -> str:
    """The finest bucket whose series stays within ``DEFAULT_MAX_POINTS`` rows.

    Fine enough to show shape, coarse enough that nothing is cut: a 5-minute
    window gets minutes, the four-day OpenStack corpus hours, BGL's seven months
    weeks.
    """
    for bucket in pred.BUCKETS:
        if _points(window_seconds, bucket, series) <= DEFAULT_MAX_POINTS:
            return bucket
    return pred.BUCKETS[-1]


@bulk_query
def get_trend(
    start_time: str,
    end_time: str,
    field: str,
    value: str | int,
    dataset: str | None = None,
    bucket: str | None = None,
    severity: str | None = None,
    group_by: str | None = None,
) -> tuple[sql.Composable, list, QueryStats]:
    """Per-bucket counts for one field value, with direction of travel.

    ``bucket=None`` picks one from the window length (:func:`choose_bucket`).
    ``group_by="severity"`` returns one series per severity level that occurs
    in the window, each zero-filled, with ``LAG`` taken within its own series.
    ``LAG`` gives each bucket its predecessor's count and a direction, which is
    what makes the result readable as a trend rather than a column of numbers.
    """
    ds = resolve(dataset)
    window = build_window(ds, start_time, end_time)

    if group_by is not None and group_by not in GROUP_BY:
        raise refuse(
            f"A trend cannot be grouped by '{group_by}'.",
            f"Group by one of: {', '.join(GROUP_BY)}, or not at all.",
            code="bad_group_by",
        )
    if group_by and not ds.has_field("severity"):
        raise refuse(
            f"'{ds.id}' has no severity field to group a trend by.",
            "Drop group_by for this dataset.",
            code="unsupported_field",
        )

    series = _series_count(ds, group_by)
    chosen = bucket is None
    if chosen:
        bucket = choose_bucket(window.seconds, series)
    if bucket not in BUCKET_SECONDS:
        raise refuse(
            f"'{bucket}' is not a time bucket.",
            f"Use one of: {', '.join(pred.BUCKETS)}.",
            code="bad_bucket",
        )

    if _points(window.seconds, bucket, series) > MAX_BUCKETS:
        coarser = COARSER_BUCKET.get(bucket)
        per_series = f" for each of {series} severity levels" if group_by else ""
        raise refuse(
            f"A {window.seconds:.0f}-second window bucketed by {bucket} would "
            f"return over {MAX_BUCKETS} points{per_series}.",
            (
                f"Bucket by {coarser} instead."
                if coarser
                else "Narrow the time window."
            ),
            code="too_many_buckets",
            max_buckets=MAX_BUCKETS,
        )

    stats = (
        plan_and_guard(ds, window)
        if ds.kind is DatasetKind.REMOTE_PARQUET
        else QueryStats(
            dataset=ds.id,
            approximate=not ds.exact_counts,
            window_seconds=window.seconds,
        )
    )
    stats.notes.append(
        f"bucketed by {bucket}"
        + (" (chosen from the window length)" if chosen else "")
        + "; every bucket in the window is listed, 0 where nothing was logged"
    )

    logger.debug(
        "Trend on %s for %s=%s by %s%s from %s to %s",
        ds.id,
        field,
        value,
        bucket,
        f" per {group_by}" if group_by else "",
        start_time,
        end_time,
    )

    clause, params = pred.where(
        pred.ts_filter(ds, window),
        pred.field_equals(ds, field, value),
        pred.severity_filter(ds, severity),
    )

    cast = sql.SQL(ds.ts_cast)
    unit = sql.Literal(bucket)
    # Cast to the window's type so generated buckets and truncated row times
    # compare equal on every corpus (the remote one is nanosecond precision).
    #
    # ``bucket_counts`` is MATERIALIZED on purpose. Inlined, the planner merges
    # it into the join with the generated grid and loses the remote scan's
    # pruning: measured on a 10-second otel_s3 window, 2.06s and a ~4M-row plan
    # estimate, against 0.19s materialized -- the same as the counts alone.
    bucket_sql = sql.SQL("CAST({expr} AS {cast})").format(
        expr=pred.bucket_expression(ds, bucket), cast=cast
    )

    grouped = group_by is not None
    sev_select = (
        sql.SQL(", {col} AS severity").format(col=ds.sql_for("severity"))
        if grouped
        else sql.SQL("")
    )
    sev_group = (
        sql.SQL(", {col}").format(col=ds.sql_for("severity"))
        if grouped
        else sql.SQL("")
    )
    # Severities come from the window's own rows, never from a declared list, so
    # a level that never occurs is not padded with zeros.
    grid_join = (
        sql.SQL("CROSS JOIN (SELECT DISTINCT severity FROM bucket_counts) AS sv")
        if grouped
        else sql.SQL("")
    )
    grid_sev = sql.SQL(", sv.severity") if grouped else sql.SQL("")
    filled_sev = sql.SQL(", g.severity") if grouped else sql.SQL("")
    join_sev = sql.SQL(" AND c.severity = g.severity") if grouped else sql.SQL("")
    out_sev = sql.SQL(", severity") if grouped else sql.SQL("")
    partition = sql.SQL("PARTITION BY severity ") if grouped else sql.SQL("")
    lag = sql.SQL("LAG(count) OVER ({p}ORDER BY bucket)").format(p=partition)

    query = sql.SQL("""
        WITH bucket_counts AS MATERIALIZED (
            SELECT {bucket} AS bucket{sev_select}, count(*) AS count
            FROM {relation}
            {clause}
            GROUP BY {bucket}{sev_group}
        ),
        grid AS (
            SELECT s.bucket{grid_sev}
            FROM generate_series(
                date_trunc({unit}, %s::{cast}), %s::{cast},
                CAST({step} AS INTERVAL)
            ) AS s(bucket)
            {grid_join}
            WHERE s.bucket < %s::{cast}
        ),
        filled AS (
            SELECT g.bucket{filled_sev}, COALESCE(c.count, 0) AS count
            FROM grid g
            LEFT JOIN bucket_counts c ON c.bucket = g.bucket{join_sev}
        )
        SELECT
            bucket{out_sev},
            count,
            {lag} AS prev_count,
            CASE
                WHEN {lag} IS NULL THEN 'initial'
                WHEN count > {lag} THEN 'up'
                WHEN count < {lag} THEN 'down'
                ELSE 'flat'
            END AS trend
        FROM filled
        ORDER BY bucket{out_sev}
        {limit}
    """).format(
        bucket=bucket_sql,
        sev_select=sev_select,
        sev_group=sev_group,
        relation=ds.scan_relation(),
        clause=clause,
        grid_sev=grid_sev,
        unit=unit,
        cast=cast,
        step=sql.Literal(f"1 {bucket}"),
        grid_join=grid_join,
        filled_sev=filled_sev,
        join_sev=join_sev,
        out_sev=out_sev,
        lag=lag,
        limit=sql.SQL("LIMIT {n}").format(n=sql.Literal(MAX_BUCKETS)),
    )
    # The grid's bounds follow the filter's: start, end, then end again for the
    # half-open upper bound.
    return query, params + [window.start, window.end, window.end], stats
