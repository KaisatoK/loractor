"""Search and single-row fetch.

Search has four shapes, chosen by declared capability rather than by dataset id:

* **Lexical** -- corpora with only an inverted index use one BM25 branch. This
  is the OpenStack and BGL path today.
* **Template** -- corpora with template embeddings fuse the BM25 branch with a
  template-level semantic branch by reciprocal rank fusion.
* **Hybrid** -- corpora with a per-row embedding column fuse a BM25 branch and a
  vector branch, with the same ``1/(60 + rank)`` weighting.
* **Index only** -- an index built in place over remote files (``otel_slice``)
  answers on its own; there is no base table to join back to.

Search runs against the **index relation**, so a text query never scans S3.

Filters go inside the ranked branch
-----------------------------------

Each branch keeps only its top ``BRANCH_DEPTH`` rows before fusion. Severity
used to be applied *after* that cut, on the joined table, so a severity whose
rows ranked below the top 100 vanished. Measured: OpenStack 'instance' at ERROR
returned 0 of its 24 matches, and BGL 'error' at FATAL 0 of 345,086. Severity is
now matched in the index (``"Level" @@ %s``) inside every branch, and a time
window joins the base table inside the branch. The local index does not carry
the timestamp, so the window needs that join. Measured on BGL, the
join was correct and kept BM25 ranking: 43 ms for the whole seven-month span. A
``LineId IN (subquery)`` shape was equally correct and fast; the join was kept
because it reads the same whatever the record id. The outer query keeps both
filters as well, so a row outside them can never be returned.

Counting the matches
--------------------

:func:`search_with_total` runs the search, then :func:`count_search_matches`
with the same match condition and filters and no ``LIMIT``, so a result can say
"10 shown of 194" and ``truncated`` is exact. Without a window the count reads
only the index (~2 ms). Semantic matches have no countable match set, so a
search that fuses a semantic branch reports ``total_matches`` as unknown.
"""

from __future__ import annotations

import logging
import re

from psycopg2 import sql

from backend.db import predicates as pred
from backend.db.config import (
    EMBEDDING_DIMENSION,
    EMBEDDING_MODEL,
    EMBEDDING_SECRET,
)
from backend.db.connection import query_runner
from backend.db.datasets import Capability, Dataset, resolve
from backend.db import templates
from backend.db.errors import refuse
from backend.db.stats import QueryResult, QueryStats
from backend.db.windows import TimeWindow, build_window

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

# Search modes whose rows come partly from a semantic branch. Their matches have
# no countable set, so ``total_matches`` stays unknown.
SEMANTIC_MODES = frozenset({"hybrid", "template"})

# Alias of the base table joined inside a ranked branch for the window.
_BRANCH_TABLE = "bt"


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
    """The canonical fields a search result should show, in display order.

    ``record_id`` (OpenStack ``line_id``) comes first when the corpus has one: the
    engineer playbook fetches a hit's neighbours by it.
    """
    wanted = ("record_id", "ts", "severity", "service", "entity_id", "message", "event_id")
    return [
        (name, dataset.sql_for(name)) for name in wanted if dataset.has_field(name)
    ]


# --- Shared pieces ------------------------------------------------------------
def search_mode(dataset: Dataset) -> str:
    """Which search shape a dataset gets: index_only, hybrid, template or lexical.

    Chosen by what the corpus has, not by its name. Per-row embeddings win when
    they exist; template embeddings are the affordable substitute at scale; BM25
    alone is the floor. ``has_template_vectors`` is cached per process.
    """
    if dataset.search_is_self_contained:
        return "index_only"
    if dataset.supports(Capability.VECTOR):
        return "hybrid"
    if dataset.supports(Capability.TEMPLATES) and templates.has_template_vectors(dataset):
        return "template"
    return "lexical"


def _terms_or_refuse(query_text: str) -> tuple[str, ...]:
    terms = search_terms(query_text)
    if not terms:
        raise refuse(
            f"No searchable words in {query_text!r}.",
            "Search for a word that appears in log messages, such as "
            "'timeout' or 'connection refused'.",
            code="empty_query",
        )
    return terms


def _matcher(terms: tuple[str, ...]) -> sql.Composable:
    """One fuzzy sub-query per word, combined with ``ts_any``.

    The index stores *tokens*, so a multi-word argument is looked up as a
    single token and matches nothing. Measured against the slice: 'order'
    matched 58,199 rows while 'failed to place order' matched 0. ``ts_any``
    keeps recall (any word may match) and lets BM25 float the rows matching more
    words to the top.
    """
    return sql.SQL("ts_any([{terms}])").format(
        terms=sql.SQL(", ").join(
            sql.SQL("ts_levenshtein({t}, 1)").format(t=sql.Placeholder())
            for _ in terms
        )
    )


def search_window(
    dataset: Dataset, start_time: str | None, end_time: str | None
) -> TimeWindow | None:
    """The optional search window: both bounds or neither, validated as usual."""
    if start_time is None and end_time is None:
        return None
    if start_time is None or end_time is None:
        raise refuse(
            "A search window needs both start_time and end_time.",
            "Give both bounds, or neither to search the whole index.",
            code="half_window",
        )
    return build_window(dataset, start_time, end_time)


def _qualified(alias: str, column: sql.Composable) -> sql.Composable:
    return sql.SQL("{a}.{c}").format(a=sql.Identifier(alias), c=column)


def _branch_filters(
    dataset: Dataset,
    alias: str,
    severity: str | None,
    window: TimeWindow | None,
) -> tuple[sql.Composable, list[sql.Composable], list]:
    """``(join, where clauses, params)`` filtering one ranked branch.

    ``alias`` names the index relation in that branch. Severity is matched in
    the index when the corpus indexes it verbatim -- no join needed -- and
    otherwise on the base table. A window always needs the base table, since
    the local index does not carry the timestamp.
    """
    _, severity_params = pred.severity_filter(dataset, severity)  # validates
    clauses: list[sql.Composable] = []
    params: list = []
    needs_table = window is not None

    if severity_params:
        field = dataset.field("severity")
        if field.text_indexed:
            clauses.append(
                sql.SQL("{col} @@ %s").format(
                    col=_qualified(alias, dataset.sql_for("severity"))
                )
            )
        else:
            needs_table = True
            clauses.append(
                sql.SQL("{col} = %s").format(
                    col=_qualified(_BRANCH_TABLE, dataset.sql_for("severity"))
                )
            )
        params.extend(severity_params)

    if window is not None:
        clause, window_params = pred.ts_filter(dataset, window, alias=_BRANCH_TABLE)
        clauses.append(clause)
        params.extend(window_params)

    join: sql.Composable = sql.SQL("")
    if needs_table:
        record_id = dataset.sql_for("record_id")
        join = sql.SQL("JOIN {relation} {bt} ON {bt_id} = {idx_id}").format(
            relation=dataset.scan_relation(),
            bt=sql.Identifier(_BRANCH_TABLE),
            bt_id=_qualified(_BRANCH_TABLE, record_id),
            idx_id=_qualified(alias, record_id),
        )
    return join, clauses, params


def _and(clauses: list[sql.Composable], *, prefix: str) -> sql.Composable:
    """Clauses joined with AND, after ``prefix`` ('WHERE' or 'AND'); empty if none."""
    if not clauses:
        return sql.SQL("")
    return sql.SQL(prefix + " ") + sql.SQL(" AND ").join(clauses)


# --- Search ---------------------------------------------------------------------
@query_runner
def search_logs(
    query_text: str,
    dataset: str | None = None,
    severity: str | None = None,
    limit: int = 10,
    start_time: str | None = None,
    end_time: str | None = None,
) -> tuple[sql.Composable, list, QueryStats]:
    """Rank log lines by relevance to ``query_text``.

    The window is optional: search reads the inverted index, whose size is
    already bounded by whatever built it, so a window narrows the question
    rather than bounding the cost. Use :func:`search_with_total` to also count
    every match.
    """
    ds = resolve(dataset)
    pred.require(ds, Capability.BM25, "full-text search")
    terms = _terms_or_refuse(query_text)
    window = search_window(ds, start_time, end_time)

    stats = QueryStats(dataset=ds.id, approximate=False)
    if window is not None:
        stats.window_seconds = window.seconds

    mode = search_mode(ds)
    logger.debug(
        'Searching %s for "%s" (severity=%s, window=%s, mode=%s)',
        ds.id,
        query_text,
        severity,
        f"{window.start}..{window.end}" if window else None,
        mode,
    )

    # An external index answers on its own: there is no base table to join back
    # to, and the index materializes columns from the source files itself. So
    # the fusion machinery below -- which exists to join ranked ids against a
    # table -- has nothing to join to, and would be wrong rather than slow.
    if mode == "index_only":
        return _search_index_only(ds, terms, severity, window, limit, stats)

    record_id = ds.sql_for("record_id")
    message = ds.sql_for("message")
    index = ds.search_relation()
    relation = ds.scan_relation()
    matcher = _matcher(terms)

    lex_join, lex_clauses, lex_params = _branch_filters(ds, "i", severity, window)
    lexical = sql.SQL("""
        SELECT rid, ROW_NUMBER() OVER (ORDER BY s DESC) AS rank
        FROM (
            SELECT {idx_id} AS rid, BM25({i}.tableoid) AS s
            FROM {index} {i} {join}
            WHERE {msg} @@ {matcher} {filters}
            ORDER BY s DESC
            LIMIT {depth}
        ) lex
    """).format(
        idx_id=_qualified("i", record_id),
        i=sql.Identifier("i"),
        index=index,
        join=lex_join,
        msg=_qualified("i", message),
        matcher=matcher,
        filters=_and(lex_clauses, prefix="AND"),
        depth=sql.Literal(BRANCH_DEPTH),
    )
    params: list = [*terms, *lex_params]

    if mode == "hybrid":
        # The embedding is computed inside SereneDB; the model name and
        # dimension are code-owned configuration, not caller input.
        vec_join, vec_clauses, vec_params = _branch_filters(ds, "i", severity, window)
        fused = sql.SQL("""
            WITH query_embedding AS (
                SELECT ai_embed(%s, {model}, {secret})::FLOAT[{dim}] AS emb
            ),
            fused AS (
                {lexical}
                UNION ALL
                SELECT rid, ROW_NUMBER() OVER (ORDER BY dist) AS rank
                FROM (
                    SELECT {idx_id} AS rid, {i}.embedding <=> q.emb AS dist
                    FROM {index} {i} {join} CROSS JOIN query_embedding q
                    {filters}
                    ORDER BY dist
                    LIMIT {depth}
                ) vec
            )
        """).format(
            model=sql.Literal(EMBEDDING_MODEL),
            secret=sql.Literal(EMBEDDING_SECRET),
            dim=sql.Literal(EMBEDDING_DIMENSION),
            lexical=lexical,
            idx_id=_qualified("i", record_id),
            i=sql.Identifier("i"),
            index=index,
            join=vec_join,
            filters=_and(vec_clauses, prefix="WHERE"),
            depth=sql.Literal(BRANCH_DEPTH),
        )
        # ai_embed's placeholder comes first in the composed statement, then the
        # lexical branch's, then the vector branch's filters.
        params = [query_text, *params, *vec_params]
    elif mode == "template":
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
        tpl_join, tpl_clauses, tpl_params = _branch_filters(ds, "r", severity, window)
        template_sql, _ = templates.template_branch(
            ds, join=tpl_join, where=_and(tpl_clauses, prefix="WHERE")
        )
        fused = sql.SQL("""
            WITH query_embedding AS (
                SELECT ai_embed(%s, {model}, {secret})::FLOAT[{dim}] AS emb
            ),
            fused AS (
                {lexical}
                UNION ALL
                {template_branch}
            )
        """).format(
            model=sql.Literal(EMBEDDING_MODEL),
            secret=sql.Literal(EMBEDDING_SECRET),
            dim=sql.Literal(EMBEDDING_DIMENSION),
            lexical=lexical,
            template_branch=template_sql,
        )
        params = [query_text, *params, *tpl_params]
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

    # The outer filters repeat the branch filters on the joined row: a guard, so
    # a row outside them can never be returned whatever a branch did.
    outer_clauses: list[sql.Composable] = []
    outer_params: list = []
    _, severity_params = pred.severity_filter(ds, severity)
    if severity_params:
        outer_clauses.append(
            sql.SQL("{col} = %s").format(col=_qualified("t", ds.sql_for("severity")))
        )
        outer_params.extend(severity_params)
    if window is not None:
        clause, window_params = pred.ts_filter(ds, window, alias="t")
        outer_clauses.append(clause)
        outer_params.extend(window_params)

    query = sql.SQL("""
        {fused}
        SELECT {columns},
               SUM(1.0 / ({k} + f.rank)) AS relevance
        FROM fused f
        JOIN {relation} t ON t.{record_id} = f.rid
        {outer_filters}
        GROUP BY {columns_bare}
        ORDER BY relevance DESC
        {limit}
    """).format(
        fused=fused,
        columns=sql.SQL(", ").join(select_columns),
        k=sql.Literal(RRF_K),
        relation=relation,
        record_id=record_id,
        outer_filters=_and(outer_clauses, prefix="WHERE"),
        columns_bare=sql.SQL(", ").join(
            sql.SQL("t.{col}").format(col=column)
            for _, column in _display_fields(ds)
        ),
        limit=pred.bounded_limit(ds, limit),
    )
    return query, params + outer_params, stats


@query_runner
def count_search_matches(
    query_text: str,
    dataset: str | None = None,
    severity: str | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
) -> tuple[sql.Composable, list, QueryStats]:
    """Every row :func:`search_logs` could rank: the same match and filters, no LIMIT.

    The lexical match only. Without a window this reads the index alone
    (measured ~2 ms on 4.7M BGL rows); a window joins the base table as the
    ranked branch does (~37 ms for BGL's whole span).
    """
    ds = resolve(dataset)
    pred.require(ds, Capability.BM25, "full-text search")
    terms = _terms_or_refuse(query_text)
    window = search_window(ds, start_time, end_time)
    stats = QueryStats(dataset=ds.id, approximate=False)

    if ds.search_is_self_contained:
        clauses, params = _index_only_filters(ds, terms, severity, window)
        query = sql.SQL("SELECT count(*) AS n FROM {index} WHERE {where}").format(
            index=ds.search_relation(), where=sql.SQL(" AND ").join(clauses)
        )
        return query, params, stats

    join, clauses, filter_params = _branch_filters(ds, "i", severity, window)
    query = sql.SQL("""
        SELECT count(*) AS n
        FROM {index} {i} {join}
        WHERE {msg} @@ {matcher} {filters}
    """).format(
        index=ds.search_relation(),
        i=sql.Identifier("i"),
        join=join,
        msg=_qualified("i", ds.sql_for("message")),
        matcher=_matcher(terms),
        filters=_and(clauses, prefix="AND"),
    )
    return query, [*terms, *filter_params], stats


def search_with_total(
    query_text: str,
    dataset: str | None = None,
    severity: str | None = None,
    limit: int = 10,
    start_time: str | None = None,
    end_time: str | None = None,
) -> QueryResult:
    """Search, then count every match, so ``truncated`` is exact.

    Two statements, both captured for the tool result's ``query``. A refused
    search is returned as is, uncounted. A search that fuses a semantic branch
    reports ``total_matches`` as unknown: its rows are not a countable set.
    """
    result = search_logs(query_text, dataset, severity, limit, start_time, end_time)
    if result.refused:
        return result
    if search_mode(resolve(dataset)) in SEMANTIC_MODES:
        result.stats.notes.append(
            "total_matches not counted: semantic matches are not a countable set"
        )
        return result

    counted = count_search_matches(query_text, dataset, severity, start_time, end_time)
    if counted.refused or not counted:
        return result
    result.total_matches = int(counted[0]["n"])
    result.stats.elapsed_ms += counted.stats.elapsed_ms
    return result


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
        if name != "record_id"  # selected first, below
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


# --- The in-place index ---------------------------------------------------------
def _index_only_filters(
    dataset: Dataset,
    terms: tuple[str, ...],
    severity: str | None,
    window: TimeWindow | None,
) -> tuple[list[sql.Composable], list]:
    """Match, severity and window clauses read straight off a self-contained index.

    Severity is matched through the index, which carries it verbatim. The
    timestamp is not indexed: each candidate row's value is re-read from its
    source file, so a window here is untested against a built index and may be
    slow (AGENTS.md).
    """
    clauses = [
        sql.SQL("{m} @@ {matcher}").format(
            m=dataset.sql_for("message"), matcher=_matcher(terms)
        )
    ]
    params: list = list(terms)
    _, severity_params = pred.severity_filter(dataset, severity)
    if severity_params:
        clauses.append(sql.SQL("{col} @@ %s").format(col=dataset.sql_for("severity")))
        params.extend(severity_params)
    if window is not None:
        clause, window_params = pred.ts_filter(dataset, window)
        clauses.append(clause)
        params.extend(window_params)
    return clauses, params


def _search_index_only(
    dataset: Dataset,
    terms: tuple[str, ...],
    severity: str | None,
    window: TimeWindow | None,
    limit: int,
    stats: QueryStats,
) -> tuple[sql.Composable, list, QueryStats]:
    """BM25 search straight off an index, with no base table.

    Used for an index built over remote files. Columns are read through the
    index, which re-fetches each matched row from its file, so this is the one
    search path whose cost is dominated by object storage rather than by the
    index.

    Note the timestamp goes through ``select_sql_for``: materializing a
    nanosecond value raises in the driver exactly as reading it directly does.
    """
    index = dataset.search_relation()
    columns = sql.SQL(", ").join(
        sql.SQL("{col} AS {alias}").format(
            col=dataset.select_sql_for(name), alias=sql.Identifier(name)
        )
        for name, _ in _display_fields(dataset)
    )
    clauses, params = _index_only_filters(dataset, terms, severity, window)

    query = sql.SQL("""
        SELECT {columns}, BM25({index}.tableoid) AS relevance
        FROM {index}
        WHERE {where}
        ORDER BY relevance DESC
        {limit}
    """).format(
        columns=columns,
        index=index,
        where=sql.SQL(" AND ").join(clauses),
        limit=pred.bounded_limit(dataset, limit),
    )

    stats.notes.append(
        "rows were read from object storage through an in-place index; "
        "nothing was copied locally"
    )
    return query, params, stats
