"""Trend queries -- how one entity's volume moves over time.

The original three functions bucketed on ``EXTRACT(MINUTE FROM log_time)``,
which has two problems: it folds every hour onto the same sixty buckets, so the
series is only meaningful inside a single hour, and ``log_time`` is an
OpenStack-only column that the OTel corpus does not have.

Both are fixed by ``date_trunc`` on the canonical timestamp with an explicit
bucket, so a trend can span seconds or days and reads the same on every corpus.
"""

from __future__ import annotations

import logging

from psycopg2 import sql

from backend.db import predicates as pred
from backend.db.connection import bulk_query
from backend.db.datasets import DatasetKind, resolve
from backend.db.pruning import plan_and_guard
from backend.db.stats import QueryStats
from backend.db.windows import build_window

logger = logging.getLogger(__name__)

# Buckets per trend. More than this and the series is noise rather than a trend,
# and the result stops fitting in a tool result the model can read.
MAX_BUCKETS = 240


@bulk_query
def get_trend(
    start_time: str,
    end_time: str,
    field: str,
    value: str | int,
    dataset: str | None = None,
    bucket: str = "minute",
    severity: str | None = None,
) -> tuple[sql.Composable, list, QueryStats]:
    """Per-bucket counts for one field value, with direction of travel.

    ``LAG`` gives each bucket its predecessor's count and a direction, which is
    what makes the result readable as a trend rather than a column of numbers.
    """
    ds = resolve(dataset)
    window = build_window(ds, start_time, end_time)

    bucket_seconds = {"second": 1, "minute": 60, "hour": 3600, "day": 86400}[
        bucket if bucket in ("second", "minute", "hour", "day") else "minute"
    ]
    if window.seconds / bucket_seconds > MAX_BUCKETS:
        from backend.db.errors import refuse

        coarser = {"second": "minute", "minute": "hour", "hour": "day"}.get(bucket)
        raise refuse(
            f"A {window.seconds:.0f}-second window bucketed by {bucket} would "
            f"return over {MAX_BUCKETS} points.",
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

    logger.debug(
        "Trend on %s for %s=%s by %s from %s to %s",
        ds.id,
        field,
        value,
        bucket,
        start_time,
        end_time,
    )

    clause, params = pred.where(
        pred.ts_filter(ds, window),
        pred.field_equals(ds, field, value),
        pred.severity_filter(ds, severity),
    )

    query = sql.SQL("""
        WITH bucket_counts AS (
            SELECT {bucket} AS bucket, count(*) AS count
            FROM {relation}
            {clause}
            GROUP BY {bucket}
        )
        SELECT
            bucket,
            count,
            LAG(count) OVER (ORDER BY bucket) AS prev_count,
            CASE
                WHEN LAG(count) OVER (ORDER BY bucket) IS NULL THEN 'initial'
                WHEN count > LAG(count) OVER (ORDER BY bucket) THEN 'up'
                WHEN count < LAG(count) OVER (ORDER BY bucket) THEN 'down'
                ELSE 'flat'
            END AS trend
        FROM bucket_counts
        ORDER BY bucket
        {limit}
    """).format(
        bucket=pred.bucket_expression(ds, bucket),
        relation=ds.scan_relation(),
        clause=clause,
        limit=sql.SQL("LIMIT {n}").format(n=sql.Literal(MAX_BUCKETS)),
    )
    return query, params, stats
