"""One trace's log lines across services, from the remote OpenTelemetry corpus.

The OTel demo app tags the log lines of a request with the request's ``TraceId``
(and the ``SpanId`` of the span that wrote them), so one failing line can be
followed through every service it touched: the frontend call, the downstream
service that failed and what that service logged just before. This is the
log-to-trace correlation the corpus supports; it carries no span timings or
parent links, only the log lines that name the trace.

The corpus is Parquet in object storage, sorted by timestamp, so the time window
is the cost control exactly as everywhere else on the remote tier: the footers
prune the window to a few row groups (``plan_and_guard``), and the trace id is an
equality filter inside it. A trace's lines sit within seconds of one another, so a
window around a search hit's timestamp is enough.

Only lines that carry a trace id can appear. A service that does not set one has
no lines here, however much it logged in that window; the result says so rather
than implying the trace is complete. Rows are examples (``kind=context``), never a
frequency.
"""

from __future__ import annotations

import logging

from psycopg2 import sql

from backend.db import predicates as pred
from backend.db.connection import query_runner
from backend.db.datasets import ENTITY_ID, DatasetKind, resolve
from backend.db.errors import refuse
from backend.db.pruning import plan_and_guard
from backend.db.stats import QueryResult, QueryStats
from backend.db.windows import build_window

logger = logging.getLogger(__name__)

# The column holding the span a line was written in. Not a canonical field of the
# registry (only this corpus has it), so the query names it here, not from a tool
# argument.
SPAN_COLUMN = "SpanId"

# A trace is a handful of lines; this is generous and below the corpus' own row cap.
MAX_TRACE_LINES = 200


@query_runner
def trace_logs(
    trace_id: str,
    start_time: str,
    end_time: str,
    dataset: str | None = "otel_s3",
    limit: int = 100,
) -> tuple[sql.Composable, list, QueryStats]:
    """The lines of ``trace_id`` inside the window, oldest first."""
    ds = resolve(dataset)
    if ds.kind is not DatasetKind.REMOTE_PARQUET or not ds.has_field(ENTITY_ID):
        raise refuse(
            f"'{ds.id}' has no trace-structured log lines to follow.",
            "Use dataset 'otel_s3', the remote OpenTelemetry corpus; the other "
            "corpora have no trace id.",
            code="unsupported_field",
            field=ENTITY_ID,
        )
    if not 1 <= limit <= MAX_TRACE_LINES:
        raise refuse(
            f"limit={limit} is outside 1..{MAX_TRACE_LINES}.",
            f"Ask for between 1 and {MAX_TRACE_LINES} lines.",
            code="limit_exceeded",
        )

    window = build_window(ds, start_time, end_time)
    stats = plan_and_guard(ds, window)

    clause, params = pred.where(
        pred.ts_filter(ds, window), pred.field_equals(ds, ENTITY_ID, trace_id)
    )
    query = sql.SQL("""
        SELECT {ts} AS ts,
               {severity} AS severity,
               {service} AS service,
               {span} AS span_id,
               {message} AS message
        FROM {relation}
        {clause}
        ORDER BY {order_ts}
        LIMIT {limit}
    """).format(
        ts=ds.select_sql_for("ts"),
        severity=ds.sql_for("severity"),
        service=ds.sql_for("service"),
        span=sql.Identifier(SPAN_COLUMN),
        message=ds.sql_for("message"),
        relation=ds.scan_relation(),
        clause=clause,
        order_ts=ds.sql_for("ts"),
        limit=sql.Literal(limit),
    )
    logger.debug("Trace %s on %s in %s..%s", trace_id, ds.id, start_time, end_time)
    return query, params, stats


def get_trace_logs(
    trace_id: str,
    start_time: str,
    end_time: str,
    dataset: str | None = "otel_s3",
    limit: int = 100,
) -> QueryResult:
    """:func:`trace_logs`, plus ``t_plus_s`` per line and notes that explain the result.

    ``t_plus_s`` is the time from the trace's first returned line, computed here
    from the timestamps the database returned, so the order and the gaps do not
    depend on a model reading timestamps.
    """
    result = trace_logs(trace_id, start_time, end_time, dataset, limit)
    if result.refused:
        return result

    rows = list(result)
    if not rows:
        result.stats.notes.append(
            f"No line in the window carries trace id {trace_id}. The id may be "
            "wrong, the lines may fall outside the window (a trace's lines sit "
            "within seconds of one another, so centre the window on a line of "
            "the trace), or its services set no trace id."
        )
        return result

    first = rows[0]["ts"]
    for row in rows:
        try:
            row["t_plus_s"] = round((row["ts"] - first).total_seconds(), 6)
        except TypeError:  # a driver that returned text instead of datetimes
            row["t_plus_s"] = None

    services = sorted({row["service"] for row in rows if row["service"]})
    result.stats.notes.append(
        f"{len(rows)} line(s) of this trace across {len(services)} service(s) "
        f"({', '.join(services)}) in the window. Only lines that carry a trace "
        "id appear: a service that sets none has no lines here even if it "
        "logged during the request."
    )
    if len(rows) >= limit:
        result.stats.notes.append(
            f"The result stopped at limit={limit}; later lines of the trace may "
            "exist. Raise the limit or narrow the window."
        )
    return result
