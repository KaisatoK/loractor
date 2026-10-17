"""Value-discovery queries -- "what services exist?" -- always bounded.

The original ``get_pids`` / ``get_services`` / ``get_event_ids`` ran
``SELECT DISTINCT <col> FROM log ORDER BY <col>`` with **no limit**. That is
harmless over 2,000 rows and ruinous over a billion, especially for a
high-cardinality field like ``TraceId`` where the distinct set is most of the
corpus.

The replacement is one function with a mandatory window, a mandatory limit, and
``ORDER BY count DESC`` so the values that come back are the ones worth knowing
about rather than the alphabetically first.
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

# Fields whose distinct set is so large that listing values is meaningless.
# ``entity_id`` on the OTel corpus is a trace id: a billion rows hold hundreds
# of millions of distinct values, and the top-N by volume says nothing useful.
HIGH_CARDINALITY: dict[str, tuple[str, ...]] = {
    "otel_s3": ("entity_id",),
    "otel_slice": ("entity_id",),
}


@bulk_query
def get_field_values(
    start_time: str,
    end_time: str,
    field: str,
    dataset: str | None = None,
    limit: int = 50,
) -> tuple[sql.Composable, list, QueryStats]:
    """The most common values of ``field`` in the window, with their counts.

    Ranked by volume, not alphabetically: the agent uses this to pick a real
    filter value before asking for a trend, so the useful answer is "which
    services produce the most rows", not "which service name sorts first".
    """
    ds = resolve(dataset)

    if field in HIGH_CARDINALITY.get(ds.id, ()):
        from backend.db.errors import refuse

        raise refuse(
            f"'{field}' on '{ds.id}' has far too many distinct values to list.",
            "Filter by service or severity instead, or search the message text "
            "to find a specific identifier.",
            code="high_cardinality",
        )

    window = build_window(ds, start_time, end_time)
    stats = (
        plan_and_guard(ds, window)
        if ds.kind is DatasetKind.REMOTE_PARQUET
        else QueryStats(
            dataset=ds.id,
            approximate=not ds.exact_counts,
            window_seconds=window.seconds,
        )
    )

    logger.debug("Listing %s values on %s (limit %s)", field, ds.id, limit)
    clause, params = pred.where(pred.ts_filter(ds, window))

    query = sql.SQL("""
        SELECT {col} AS value, count(*) AS count
        FROM {relation}
        {clause}
        GROUP BY {col}
        ORDER BY count DESC, value
        {limit}
    """).format(
        col=ds.sql_for(field),
        relation=ds.scan_relation(),
        clause=clause,
        limit=pred.bounded_limit(ds, limit),
    )
    return query, params, stats
