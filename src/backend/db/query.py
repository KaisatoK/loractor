"""Search and single-row fetch.

Search has two shapes, chosen by declared capability rather than by dataset id:

* **Hybrid** -- corpora with a per-row embedding column fuse a BM25 branch and a
  vector branch with reciprocal rank fusion. This is the OpenStack path and it
  is preserved exactly, down to the ``1/(60 + rank)`` weighting.
* **Lexical** -- corpora with only an inverted index use the BM25 branch alone.
  This is the OTel slice, which deliberately has no per-row embeddings: there is
  no budget to embed a billion log bodies, and template-level embeddings
  (:mod:`backend.db.templates`) cover semantic search instead.

Search always runs against the **index relation**, never the base table or the
remote files, so a text query never reaches S3.
"""

from __future__ import annotations

import logging

from psycopg2 import sql

from backend.db import predicates as pred
from backend.db.config import EMBEDDING_DIMENSION, EMBEDDING_MODEL
from backend.db.connection import query_runner
from backend.db.datasets import Capability, Dataset, resolve
from backend.db.stats import QueryStats

logger = logging.getLogger(__name__)

# Rows each branch contributes to the fusion before ranking. 100 is the original
# value and is kept so OpenStack results are unchanged.
BRANCH_DEPTH = 100

# Reciprocal-rank-fusion constant, also unchanged from the original.
RRF_K = 60


def _display_fields(dataset: Dataset) -> list[tuple[str, sql.Composable]]:
    """The canonical fields a search result should show, in display order."""
    wanted = ("ts", "severity", "service", "entity_id", "message", "event_id")
    return [
        (name, dataset.sql_for(name)) for name in wanted if dataset.has_field(name)
    ]


@query_runner
def search_logs(
    query_text: str,
    dataset: str | None = None,
    severity: str | None = None,
    limit: int = 10,
) -> tuple[sql.Composable, list, QueryStats]:
    """Rank log lines by relevance to ``query_text``.

    No time window is required: this reads the inverted index, whose size is
    already bounded by whatever built it, so there is no scan to constrain.
    """
    ds = resolve(dataset)
    pred.require(ds, Capability.BM25, "full-text search")

    stats = QueryStats(dataset=ds.id, approximate=False)
    hybrid = ds.supports(Capability.VECTOR)
    logger.debug(
        'Searching %s for "%s" (severity=%s, hybrid=%s)',
        ds.id,
        query_text,
        severity,
        hybrid,
    )

    record_id = ds.sql_for("record_id")
    message = ds.sql_for("message")
    index = ds.search_relation()
    relation = ds.scan_relation()

    lexical = sql.SQL("""
        SELECT {record_id} AS rid,
               ROW_NUMBER() OVER (ORDER BY s DESC) AS rank
        FROM (
            SELECT {record_id}, BM25({index}.tableoid) AS s
            FROM {index}
            WHERE {message} @@ ts_levenshtein(%s, 1)
            ORDER BY s DESC
            LIMIT {depth}
        ) lex
    """).format(
        record_id=record_id,
        index=index,
        message=message,
        depth=sql.Literal(BRANCH_DEPTH),
    )
    params: list = [query_text]

    if hybrid:
        # The embedding is computed inside SereneDB; the model name and
        # dimension are code-owned configuration, not caller input.
        fused = sql.SQL("""
            WITH query_embedding AS (
                SELECT ai_embed(%s, {model}, 'local_ai')::FLOAT[{dim}] AS emb
            ),
            fused AS (
                {lexical}
                UNION ALL
                SELECT {record_id} AS rid,
                       ROW_NUMBER() OVER (ORDER BY dist) AS rank
                FROM (
                    SELECT {record_id}, embedding <=> q.emb AS dist
                    FROM {index}, query_embedding q
                    ORDER BY dist
                    LIMIT {depth}
                ) vec
            )
        """).format(
            model=sql.Literal(EMBEDDING_MODEL),
            dim=sql.Literal(EMBEDDING_DIMENSION),
            lexical=lexical,
            record_id=record_id,
            index=index,
            depth=sql.Literal(BRANCH_DEPTH),
        )
        # ai_embed's placeholder comes first in the composed statement.
        params = [query_text, query_text]
    else:
        fused = sql.SQL("WITH fused AS ({lexical})").format(lexical=lexical)

    select_columns = [
        sql.SQL("t.{col} AS {alias}").format(
            col=column, alias=sql.Identifier(name)
        )
        if isinstance(column, sql.Identifier)
        else sql.SQL("{col} AS {alias}").format(
            col=column, alias=sql.Identifier(name)
        )
        for name, column in _display_fields(ds)
    ]

    severity_clause, severity_params = pred.severity_filter(ds, severity)
    if severity_params:
        severity_clause = sql.SQL("WHERE t.{col} = %s").format(
            col=ds.sql_for("severity")
        )

    query = sql.SQL("""
        {fused}
        SELECT {columns},
               SUM(1.0 / ({k} + f.rank)) AS relevance
        FROM fused f
        JOIN {relation} t ON t.{record_id} = f.rid
        {severity_clause}
        GROUP BY {columns_bare}
        ORDER BY relevance DESC
        {limit}
    """).format(
        fused=fused,
        columns=sql.SQL(", ").join(select_columns),
        k=sql.Literal(RRF_K),
        relation=relation,
        record_id=record_id,
        severity_clause=severity_clause,
        columns_bare=sql.SQL(", ").join(
            sql.SQL("t.{col}").format(col=column)
            for _, column in _display_fields(ds)
        ),
        limit=pred.bounded_limit(ds, limit),
    )
    return query, params + severity_params, stats


@query_runner
def get_log_by_record_id(
    record_id: int,
    dataset: str | None = None,
) -> tuple[sql.Composable, list, QueryStats]:
    """Fetch one complete row by its primary key.

    Gated on :data:`~backend.db.datasets.Capability.RECORD_ID`: the raw OTel
    corpus has no primary key, so asking for one there is refused with a pointer
    to the datasets that do, rather than failing on a missing column.
    """
    ds = resolve(dataset)
    pred.require(ds, Capability.RECORD_ID, "fetching a row by id")

    logger.debug("Fetching %s record %s", ds.id, record_id)
    columns = [
        sql.SQL("{col} AS {alias}").format(col=column, alias=sql.Identifier(name))
        for name, column in _display_fields(ds)
    ]
    if ds.has_field("template"):
        columns.append(
            sql.SQL("{col} AS template").format(col=ds.sql_for("template"))
        )

    query = sql.SQL("""
        SELECT {id_col} AS record_id, {columns}
        FROM {relation}
        WHERE {id_col} = %s
    """).format(
        id_col=ds.sql_for("record_id"),
        columns=sql.SQL(", ").join(columns),
        relation=ds.scan_relation(),
    )
    return query, [record_id], QueryStats(dataset=ds.id)
