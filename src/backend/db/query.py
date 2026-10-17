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
import re

from psycopg2 import sql

from backend.db import predicates as pred
from backend.db.config import EMBEDDING_DIMENSION, EMBEDDING_MODEL
from backend.db.connection import query_runner
from backend.db.datasets import Capability, Dataset, resolve
from backend.db import templates
from backend.db.errors import refuse
from backend.db.stats import QueryStats

logger = logging.getLogger(__name__)

# Rows each branch contributes to the fusion before ranking. 100 is the original
# value and is kept so OpenStack results are unchanged.
BRANCH_DEPTH = 100

# Reciprocal-rank-fusion constant, also unchanged from the original.
RRF_K = 60

# Longest word list a single search will match on. A pasted stack trace would
# otherwise become hundreds of fuzzy sub-queries against the index.
MAX_SEARCH_TERMS = 12

# A word is a run of letters/digits, optionally hyphenated internally. This
# splits on JSON punctuation, which log bodies are full of.
_WORD_RE = re.compile(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*")

# Words carrying no selectivity in a log corpus. Deliberately short: this trims
# obvious filler, it is not a linguistic stop-word list, and anything that might
# appear in a message (``error``, ``failed``, ``timeout``) is kept.
_SEARCH_NOISE = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "but", "by", "for", "from",
    "had", "has", "have", "in", "is", "it", "its", "of", "on", "or", "that",
    "the", "their", "then", "there", "these", "this", "to", "was", "were",
    "what", "when", "where", "which", "why", "with",
})


def search_terms(query_text: str) -> tuple[str, ...]:
    """Split a query into the words the index will be matched on.

    The inverted index stores tokens, so a phrase handed to ``ts_levenshtein``
    whole is treated as one token and matches nothing. Splitting is therefore
    not an optimisation -- it is what makes multi-word search work at all.

    Punctuation **separates** words rather than being stripped from them,
    because log bodies are frequently JSON: ``"details":"failed`` has to become
    ``details`` and ``failed``, not ``detailsfailed``.
    """
    words: list[str] = []
    seen: set[str] = set()
    for word in _WORD_RE.findall(query_text):
        lowered = word.lower()
        if len(word) < 2 or lowered in _SEARCH_NOISE or lowered in seen:
            continue
        seen.add(lowered)
        words.append(word)
    # No fallback to the raw text when everything is noise: a search for
    # "is it the" should return nothing rather than silently match the corpus.
    return tuple(words[:MAX_SEARCH_TERMS])


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

    # Three search shapes, chosen by what the corpus actually has rather than by
    # its name. Per-row embeddings win when they exist; template embeddings are
    # the affordable substitute at scale; BM25 alone is the floor.
    hybrid = ds.supports(Capability.VECTOR)
    templated = (
        not hybrid
        and ds.supports(Capability.TEMPLATES)
        and templates.has_template_vectors(ds)
    )
    logger.debug(
        'Searching %s for "%s" (severity=%s, mode=%s)',
        ds.id,
        query_text,
        severity,
        "hybrid" if hybrid else "template" if templated else "lexical",
    )

    record_id = ds.sql_for("record_id")
    message = ds.sql_for("message")
    index = ds.search_relation()
    relation = ds.scan_relation()

    terms = search_terms(query_text)
    if not terms:
        raise refuse(
            f"No searchable words in {query_text!r}.",
            "Search for a word that appears in log messages, such as "
            "'timeout' or 'connection refused'.",
            code="empty_query",
        )

    # One fuzzy sub-query per word, combined with ts_any, rather than handing the
    # whole phrase to ts_levenshtein.
    #
    # The index stores *tokens*, so a multi-word argument is looked up as a
    # single token and matches nothing. Measured against the slice: 'order'
    # matched 58,199 rows while 'failed to place order' matched 0 -- and because
    # this is the only branch for a dataset without embeddings, every multi-word
    # search returned empty. ts_any keeps recall (any word may match) and lets
    # BM25 float the rows matching more words to the top, which is what makes a
    # natural-language query behave sensibly without an embedding model.
    matcher = sql.SQL("ts_any([{terms}])").format(
        terms=sql.SQL(", ").join(
            sql.SQL("ts_levenshtein({t}, 1)").format(t=sql.Placeholder())
            for _ in terms
        )
    )

    lexical = sql.SQL("""
        SELECT {record_id} AS rid,
               ROW_NUMBER() OVER (ORDER BY s DESC) AS rank
        FROM (
            SELECT {record_id}, BM25({index}.tableoid) AS s
            FROM {index}
            WHERE {message} @@ {matcher}
            ORDER BY s DESC
            LIMIT {depth}
        ) lex
    """).format(
        record_id=record_id,
        index=index,
        message=message,
        matcher=matcher,
        depth=sql.Literal(BRANCH_DEPTH),
    )
    params: list = list(terms)

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
        # ai_embed's placeholder comes first in the composed statement, then
        # one placeholder per lexical term.
        params = [query_text, *terms]
    elif templated:
        # Template-level semantic branch: the query is matched against the
        # *shapes* of messages, then every row of a matching shape is retrieved.
        #
        # Same reciprocal-rank fusion as the per-row path, because the two
        # branches answer different questions and both are wanted. The template
        # branch generalises -- 'money conversion problem' finds 'failed to
        # convert shipping cost', which shares no words with the query and so is
        # invisible to BM25. The lexical branch discriminates -- it sees each
        # row's full text, which a template cannot, since all rows of one
        # template look identical to it.
        template_sql, _ = templates.template_branch(ds)
        fused = sql.SQL("""
            WITH query_embedding AS (
                SELECT ai_embed(%s, {model}, 'local_ai')::FLOAT[{dim}] AS emb
            ),
            fused AS (
                {lexical}
                UNION ALL
                {template_branch}
            )
        """).format(
            model=sql.Literal(EMBEDDING_MODEL),
            dim=sql.Literal(EMBEDDING_DIMENSION),
            lexical=lexical,
            template_branch=template_sql,
        )
        params = [query_text, *terms]
        stats.notes.append("semantic matching is template-level, not per-row")
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
