"""Nearby events: the log lines just before and after one anchor line.

The engineer's question is "what happened just before and after this error?".
This answers it by record id, which is file order (and on OpenStack also time
order), so the neighbours are the lines the log itself put next to the anchor,
not a time-bounded slice. Two optional filters narrow the neighbours to one
correlation handle of the anchor's:

* ``same_entity`` -- the anchor's ``entity_id`` (process id on OpenStack, node
  on BGL), to follow one process or one piece of hardware;
* ``same_request`` -- the anchor's ``request_id``, to follow one API request.
  Only a corpus that declares the field supports it; elsewhere it is refused
  rather than silently ignored.

The nearby query is one statement: the anchor is resolved in a CTE, each side is
its own ``ORDER BY record_id ... LIMIT n`` branch (so ``before`` and ``after``
are honoured independently, however sparse the matches), and ``offset`` numbers
the rows outward from the anchor. When ``same_request`` is enabled, a second
exact aggregate measures that request id's total rows, time span and component
count. The nearby query itself is measured on the full 4.7M-row BGL table at
~10 ms, also for a node with a single line, where both branches scan to the
ends of the table without a match.

Rows are examples for a timeline. They never measure frequency.
"""

from __future__ import annotations

import logging

from psycopg2 import sql

from backend.db import predicates as pred
from backend.db.connection import query_runner
from backend.db.datasets import (
    ENTITY_ID,
    RECORD_ID,
    REQUEST_ID,
    Capability,
    Dataset,
    all_datasets,
    resolve,
)
from backend.db.errors import refuse
from backend.db.stats import QueryResult, QueryStats, merge

logger = logging.getLogger(__name__)

# The most lines either side a single call may return. Matches the tool schema.
MAX_NEIGHBOURS = 50

# Row columns in display order; a field the corpus lacks is left out.
NEARBY_FIELDS = (
    "record_id",
    "ts",
    "severity",
    "service",
    "entity_id",
    "request_id",
    "message",
    "template",
)

# What each correlation flag follows, for refusals and notes.
_FLAG_FIELDS = {"same_entity": ENTITY_ID, "same_request": REQUEST_ID}


def _columns(dataset: Dataset) -> sql.Composable:
    """The row's canonical columns, unqualified.

    Every branch reads ``{relation} t, anchor a``; the anchor's own columns are
    named ``rid``/``ent``/``req``, so no unqualified name is ambiguous, and a
    derived field's expression (OpenStack's ``request_id``) resolves against
    the base table.
    """
    return sql.SQL(", ").join(
        sql.SQL("{col} AS {alias}").format(
            col=dataset.select_sql_for(name), alias=sql.Identifier(name)
        )
        for name in NEARBY_FIELDS
        if dataset.has_field(name)
    )


def _refuse_flag(dataset: Dataset, flag: str) -> None:
    field = _FLAG_FIELDS[flag]
    able = [d.id for d in all_datasets() if d.has_field(field)
            and d.supports(Capability.RECORD_ID)]
    alternative = (
        "Use same_entity to follow one process or node instead"
        if flag == "same_request" else "Drop the filter"
    )
    raise refuse(
        f"'{dataset.id}' records no {field}, so {flag} cannot be applied.",
        f"{alternative}" + (f", or use a dataset that has it: {', '.join(able)}."
                            if able else "."),
        code="unsupported_field",
        field=field,
    )


@query_runner
def nearby_events(
    line_id: int,
    dataset: str | None = None,
    before: int = 5,
    after: int = 5,
    same_entity: bool = False,
    same_request: bool = False,
) -> tuple[sql.Composable, list, QueryStats]:
    """Rows around ``line_id``, ordered by record id, with an ``offset`` column.

    ``offset`` is negative before the anchor, 0 for the anchor and positive
    after it. An unknown anchor yields no rows (see :func:`get_nearby_events`
    for the explanatory note). A NULL correlation value on the anchor matches
    nothing, so only the anchor comes back.
    """
    ds = resolve(dataset)
    pred.require(ds, Capability.RECORD_ID, "fetching nearby events")
    for flag, wanted in (("same_entity", same_entity), ("same_request", same_request)):
        if wanted and not ds.has_field(_FLAG_FIELDS[flag]):
            _refuse_flag(ds, flag)
    for name, value in (("before", before), ("after", after)):
        if not 0 <= value <= MAX_NEIGHBOURS:
            raise refuse(
                f"{name}={value} is outside 0..{MAX_NEIGHBOURS}.",
                f"Ask for at most {MAX_NEIGHBOURS} lines on each side.",
                code="limit_exceeded",
            )

    record_id = ds.sql_for(RECORD_ID)
    relation = ds.scan_relation()
    columns = _columns(ds)

    anchor_columns = [sql.SQL("{id} AS rid").format(id=record_id)]
    correlation: list[sql.Composable] = []
    if same_entity:
        anchor_columns.append(sql.SQL("{c} AS ent").format(c=ds.sql_for(ENTITY_ID)))
        correlation.append(sql.SQL("{c} = a.ent").format(c=ds.sql_for(ENTITY_ID)))
    if same_request:
        anchor_columns.append(sql.SQL("{c} AS req").format(c=ds.sql_for(REQUEST_ID)))
        correlation.append(sql.SQL("{c} = a.req").format(c=ds.sql_for(REQUEST_ID)))
    narrow = sql.SQL("").join(sql.SQL(" AND ") + c for c in correlation)
    offset = sql.Identifier("offset")  # a reserved word, so always quoted

    def side(count: int, comparison: str, direction: str, sign: str) -> sql.Composable:
        return sql.SQL("""
            SELECT s.*, {sign}ROW_NUMBER() OVER (ORDER BY s.record_id {direction}) AS {offset}
            FROM (
                SELECT {columns}
                FROM {relation} t, anchor a
                WHERE {id} {comparison} a.rid{narrow}
                ORDER BY {id} {direction}
                LIMIT {count}
            ) s
        """).format(
            sign=sql.SQL(sign),
            direction=sql.SQL(direction),
            offset=offset,
            columns=columns,
            relation=relation,
            id=record_id,
            comparison=sql.SQL(comparison),
            narrow=narrow,
            count=sql.Literal(count),
        )

    branches: list[sql.Composable] = []
    if before:
        branches.append(side(before, "<", "DESC", "-"))
    branches.append(
        sql.SQL("""
            SELECT {columns}, 0 AS {offset}
            FROM {relation} t JOIN anchor a ON {id} = a.rid
        """).format(columns=columns, offset=offset, relation=relation, id=record_id)
    )
    if after:
        branches.append(side(after, ">", "ASC", ""))

    query = sql.SQL("""
        WITH anchor AS (
            SELECT {anchor_columns} FROM {relation} WHERE {id} = %s
        )
        SELECT * FROM (
            {branches}
        ) nearby
        ORDER BY record_id
    """).format(
        anchor_columns=sql.SQL(", ").join(anchor_columns),
        relation=relation,
        id=record_id,
        branches=sql.SQL(" UNION ALL ").join(branches),
    )
    logger.debug(
        "Nearby events on %s around %s (before=%s, after=%s, entity=%s, request=%s)",
        ds.id, line_id, before, after, same_entity, same_request,
    )
    return query, [line_id], QueryStats(dataset=ds.id, approximate=False)




@query_runner
def request_context_summary(
    request_id: str,
    dataset: str | None = None,
) -> tuple[sql.Composable, list, QueryStats]:
    """Measure the size and span of one request-id context.

    OpenStack's request id is derived from ``ADDR``. This is deliberately a
    separate, exact aggregate: nearby rows are examples, while this summary
    tells the agent whether ``same_request`` is a narrow API request or a
    broad service periodic-task context.
    """
    ds = resolve(dataset)
    if not ds.has_field(REQUEST_ID):
        _refuse_flag(ds, "same_request")
    query = sql.SQL("""
        SELECT count(*) AS request_rows,
               min({ts}) AS request_first_ts,
               max({ts}) AS request_last_ts,
               count(DISTINCT {service}) AS request_components
        FROM {relation}
        WHERE {request_id} = %s
    """).format(
        ts=ds.select_sql_for("ts"),
        service=ds.sql_for("service"),
        relation=ds.scan_relation(),
        request_id=ds.sql_for(REQUEST_ID),
    )
    return query, [request_id], QueryStats(dataset=ds.id, approximate=False)


def _merge_query_stats(result: QueryResult, extra: QueryResult) -> None:
    """Add the cost and notes of a follow-up query to a tool result."""
    notes = [*result.stats.notes, *extra.stats.notes]
    result.stats = merge([result.stats, extra.stats])
    result.stats.notes.extend(notes)


def _request_summary_note(row: dict) -> str:
    """Render a measured request-context summary for the agent."""
    return (
        "request context summary: "
        f"{row['request_rows']:,} log entries from {row['request_first_ts']} "
        f"to {row['request_last_ts']} across {row['request_components']} "
        "services/components; a very broad request-id context may be a "
        "periodic service task rather than one user request."
    )
def get_nearby_events(
    line_id: int,
    dataset: str | None = None,
    before: int = 5,
    after: int = 5,
    same_entity: bool = False,
    same_request: bool = False,
) -> QueryResult:
    """:func:`nearby_events`, plus a note whenever the result needs explaining.

    An unknown anchor is a successful, empty result rather than an error, so the
    note says why it is empty. When a correlation flag is set and the anchor
    itself has no value for that field, the filter can match nothing but the
    anchor; the note says so, so an empty timeline is not read as "nothing
    happened nearby".
    """
    result = nearby_events(line_id, dataset, before, after, same_entity, same_request)
    if result.refused:
        return result
    ds_id = resolve(dataset).id
    anchor = next((row for row in result if row.get("offset") == 0), None)
    if anchor is None:
        result.stats.notes.append(
            f"No line has record_id {line_id} in '{ds_id}', so there is no "
            "anchor. Use a record_id from a search result on the same dataset."
        )
        return result
    missing = []
    for flag, wanted in (("same_entity", same_entity), ("same_request", same_request)):
        field = _FLAG_FIELDS[flag]
        if wanted and anchor.get(field) is None:
            missing.append(flag)
            result.stats.notes.append(
                f"The anchor line has no {field}, so {flag} matches no other "
                f"line; call again without {flag} for its neighbours."
            )
    applied = [f for f, w in (("same_entity", same_entity),
                              ("same_request", same_request)) if w]
    if applied and not missing and len(result) == 1 and (before or after):
        fields = " and ".join(_FLAG_FIELDS[f] for f in applied)
        result.stats.notes.append(
            f"No other line in '{ds_id}' shares the anchor's {fields}."
        )
    if same_request and anchor.get(REQUEST_ID) is not None:
        summary = request_context_summary(anchor[REQUEST_ID], ds_id)
        if not summary.refused and summary:
            _merge_query_stats(result, summary)
            result.stats.notes.append(_request_summary_note(summary[0]))
    return result
