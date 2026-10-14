"""Auto-complete suggestions from the log index's term dictionaries.

Values are matched case-insensitively wherever the typed text appears inside
them (``LIKE %query%`` over the verbatim index); when nothing matches, a
``ts_levenshtein`` fallback fuzzy-matches the query's words against the
tokenized index. Results rank values that start with the query first, then case
matches, then popularity.

A leading selector scopes the lookup to one column: ``@`` for ``service``, ``#``
for ``event_id`` and ``$`` for ``event_template`` (see ``_SELECTORS``). A bare
selector lists that column's most popular values.
"""

import logging
import re

from psycopg2 import sql

from backend.db.config import TABLE_NAME
from backend.db.connection import query_runner

logger = logging.getLogger(__name__)

_AUTO_COMPLETE_COLUMNS = ("service", "event_id", "event_template")
_LIMIT = 5
_WORD_SEPARATOR = re.compile(r"[^A-Za-z0-9]+")
_SELECTORS = {"@": "service", "#": "event_id", "$": "event_template"}


def _resolve_selector(query: str) -> tuple[str, str | None]:
    """Split a leading column selector off ``query``.

    Returns the query without the selector and the column it selects, or ``None``
    for the column when the query does not start with a selector.
    """
    column = _SELECTORS.get(query[:1])
    if column is None:
        return query, None
    return query[1:], column


def _words(query: str) -> list[str]:
    """Split ``query`` on non-alphanumerics for the per-word fuzzy fallback.

    Each word is fuzzy-matched independently, so ``nova.osapi_compute.wsgi.srever``
    still reaches the service. A query with no word characters is used as-is.
    """
    words = [word for word in _WORD_SEPARATOR.split(query) if word]
    return words or [query]


@query_runner
def auto_complete(
    query: str,
    table_name: str = TABLE_NAME,
) -> tuple[sql.Composable, list[str | int]]:
    """Return the top ``_LIMIT`` indexed values that contain ``query``.

    Suggestions are ranked so values that start with ``query`` come first, then
    values whose case matches, then values carried by more live documents. If
    nothing matches, the same ranking is applied to values with a word within a
    small edit distance of a word of ``query``, so a typo still suggests something.

    A leading selector in ``query`` (``@``, ``#`` or ``$``) scopes the lookup to
    ``service``, ``event_id`` or ``event_template`` respectively and is stripped
    before matching. A bare selector (empty query) lists that column's most popular
    values.
    """
    query, column = _resolve_selector(query)
    columns = _AUTO_COMPLETE_COLUMNS if column is None else (column,)
    index_name = sql.Identifier(f"{table_name}_idx")
    token_index = sql.Identifier(f"{table_name}_token_idx")
    contains = f"%{query}%"
    prefix = f"{query}%"
    words = _words(query)

    logger.debug("Auto-completing %r (words %s) across %s", query, words, columns)

    def exact_branch(col: str) -> sql.Composable:
        return sql.SQL("""
            SELECT unnest(ts_dict_agg({column}))   AS suggestion,
                   unnest(ts_dict_count({column})) AS count
            FROM {index_name}
            WHERE {column} ILIKE %s
        """).format(column=sql.Identifier(col), index_name=index_name)

    def fuzzy_branch(col: str) -> sql.Composable:
        # Mistyping fallback: fuzzy-match each word against the tokenized index
        # and return the row's real value. ``lower`` on both sides (the terms are
        # lower-cased by the dictionary) makes it case-insensitive.
        terms = sql.SQL(", ").join(sql.SQL("ts_levenshtein(lower(%s))") for _ in words)
        return sql.SQL("""
            SELECT {column} AS suggestion, count(*) AS count
            FROM {token_index}
            WHERE {column} @@ ts_any([{terms}])
            GROUP BY {column}
        """).format(column=sql.Identifier(col), token_index=token_index, terms=terms)

    exact = sql.SQL(" UNION ALL ").join(exact_branch(col) for col in columns)
    fuzzy = sql.SQL(" UNION ALL ").join(fuzzy_branch(col) for col in columns)

    # ``ILIKE`` keeps the dictionary match case-insensitive; the ranking keys then
    # put values that start with the typed text ahead of values that merely
    # contain it, values whose case matches ahead of case-insensitive-only
    # matches, and only then fall back to popularity.
    order_by = sql.SQL("""
        ORDER BY (suggestion ILIKE %s) DESC,
                 (suggestion LIKE %s) DESC,
                 count DESC,
                 suggestion
        LIMIT {limit}
    """).format(limit=sql.Literal(_LIMIT))

    # The fuzzy rows are consulted only when the exact match came up empty, so a
    # normal keystroke never sees an edit-distance suggestion.
    combined = sql.SQL("""
        SELECT suggestion, count FROM exact_matches
        UNION ALL
        SELECT suggestion, count FROM fuzzy_matches
        WHERE NOT EXISTS (SELECT 1 FROM exact_matches)
    """)

    if len(columns) == 1:
        query_sql = sql.SQL("""
            WITH exact_matches AS ({exact}),
                 fuzzy_matches AS ({fuzzy})
            SELECT suggestion, count
            FROM ({combined}) matches
            {order_by}
        """).format(exact=exact, fuzzy=fuzzy, combined=combined, order_by=order_by)
    else:
        query_sql = sql.SQL("""
            WITH exact_matches AS ({exact}),
                 fuzzy_matches AS ({fuzzy})
            SELECT suggestion, SUM(count)::BIGINT AS count
            FROM ({combined}) matches
            GROUP BY suggestion
            {order_by}
        """).format(exact=exact, fuzzy=fuzzy, combined=combined, order_by=order_by)

    # One ``%s`` per exact ILIKE branch, one per word per fuzzy ts_any branch, then
    # the ORDER BY's prefix ILIKE and case-sensitive LIKE.
    params = [contains] * len(columns) + words * len(columns) + [prefix, contains]
    return query_sql, params
