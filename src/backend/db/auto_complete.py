"""Auto-complete suggestions from the log index's term dictionaries.

Values are matched case-insensitively wherever the typed text appears inside
them (``LIKE %query%`` over the verbatim index); when nothing matches, a
``ts_levenshtein`` fallback fuzzy-matches the query's words against the
tokenized index. Results rank values that start with the query first, then case
matches, then popularity.

A leading selector is required and scopes the lookup to one column: ``@`` for
``service``, ``#`` for ``event_id``, ``^`` for ``event_template`` and ``!`` for
``pid`` (its text mirror ``pid_text``; see ``_SELECTORS``). A bare selector lists
that column's most popular values; a query with no leading selector matches
nothing.

Suggestions are ranked case-insensitively by default -- a value is never preferred
merely for matching the typed case. A ``!`` placed immediately after the selector
switches to a case-sensitive ranking, where values whose case matches the typed
text rank first (see :func:`_order_keys`). The modifier is stripped before
matching, so ``@!nova`` searches the same values as ``@nova``.

``$`` searches ``content``. Because ``content`` is word-indexed it is handled
separately: suggestions are whole lines rather than the single words the shared
``ts_dict_agg`` recipe would return, and each carries its ``line_id`` in place of a
count (see :func:`_content_matches`).
"""

import logging
import re

from psycopg2 import sql

from backend.db.config import TABLE_NAME
from backend.db.connection import query_runner

logger = logging.getLogger(__name__)

_LIMIT = 5
_WORD_SEPARATOR = re.compile(r"[^A-Za-z0-9]+")
_SELECTORS = {
    "@": "service",
    "#": "event_id",
    "^": "event_template",
    "!": "pid_text",
    "$": "content",
}
_NO_MATCHES = sql.SQL(
    "SELECT NULL::text AS suggestion, NULL::bigint AS count WHERE false"
)


def _resolve_selector(query: str) -> tuple[str, str | None]:
    """Split a leading column selector off ``query``.

    Returns the query without the selector and the column it selects, or ``None``
    for the column when the query does not start with a selector.
    """
    column = _SELECTORS.get(query[:1])

    if column is None:
        return query, None

    return query[1:], column


def _resolve_modifier(query: str) -> tuple[str, bool]:
    """Split a leading ``!`` case-sensitivity modifier off ``query``.

    Returns the query without the modifier and whether it was present. It sits
    right after the selector, so it only applies once the selector has been
    stripped (``@!nova`` -> query ``nova``, flag set). Without it the ranking is
    case-insensitive.
    """
    if query.startswith("!"):
        return query[1:], True
    return query, False


def _words(query: str) -> list[str]:
    """Split ``query`` on non-alphanumerics for the per-word fuzzy fallback.

    Each word is fuzzy-matched independently, so ``nova.osapi_compute.wsgi.srever``
    still reaches the service. A query with no word characters is used as-is.
    """
    words = [word for word in _WORD_SEPARATOR.split(query) if word]
    return words or [query]


def _order_keys(
    case_sensitive: bool, prefix: str, contains: str
) -> tuple[sql.Composable, list[str]]:
    """The ranking keys that order suggestions, with their params.

    Values that start with the typed text always rank first, then popularity. By
    default that is the whole story; with ``case_sensitive`` set (the ``!``
    modifier) values whose case matches the typed text rank above
    case-insensitive-only matches.
    """
    if case_sensitive:
        return (
            sql.SQL("(suggestion ILIKE %s) DESC, (suggestion LIKE %s) DESC"),
            [prefix, contains],
        )
    return sql.SQL("(suggestion ILIKE %s) DESC"), [prefix]


def _content_matches(
    index_name: sql.Identifier,
    contains: str,
    prefix: str,
    query: str,
    case_sensitive: bool = False,
) -> tuple[sql.Composable, list[str | int]]:
    """Whole-line ``content`` matches, mirroring ``search_logs``' lexical half.

    ``content`` is word-indexed, so the shared ``ts_dict_agg`` recipe would walk
    its dictionary and suggest single words. Whole lines are selected instead:
    the substring the user typed, falling back to ``search_logs``' lexical
    ``ts_levenshtein`` match when nothing contains it. Each suggestion carries the
    line's ``line_id`` -- a repeated message keeps its earliest one -- so the UI
    can insert a ``Line ID: n`` reference instead of the whole message.
    ``case_sensitive`` adds the case-matching ranking key (see
    :func:`_order_keys`).
    """
    order_keys, order_params = _order_keys(case_sensitive, prefix, contains)
    query_sql = sql.SQL("""
        WITH exact_matches AS (
            SELECT content AS suggestion, MIN(line_id) AS line_id
            FROM {index_name}
            WHERE content ILIKE %s
            GROUP BY content
        ),
        fuzzy_matches AS (
            SELECT content AS suggestion, MIN(line_id) AS line_id
            FROM {index_name}
            WHERE content @@ ts_levenshtein(%s, 1)
            GROUP BY content
        )
        SELECT suggestion, line_id
        FROM (
            SELECT suggestion, line_id FROM exact_matches
            UNION ALL
            SELECT suggestion, line_id FROM fuzzy_matches
            WHERE NOT EXISTS (SELECT 1 FROM exact_matches)
        ) matches
        ORDER BY {order_keys},
                 line_id
        LIMIT {limit}
    """).format(
        index_name=index_name, order_keys=order_keys, limit=sql.Literal(_LIMIT)
    )
    return query_sql, [contains, query] + order_params


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

    A leading selector is required: ``@`` scopes the lookup to ``service``, ``#``
    to ``event_id``, ``^`` to ``event_template`` and ``!`` to ``pid`` (via its
    text mirror ``pid_text``); ``$`` searches ``content``. The selector is stripped
    before matching. A bare selector (empty query) lists that column's most popular
    values, while a query with no leading selector matches nothing.

    Rankings are case-insensitive by default; a ``!`` immediately after the
    selector makes them case-sensitive, ranking values whose case matches the typed
    text first (``@!nova`` vs ``@nova``).
    """
    query, column = _resolve_selector(query)

    if column is None:
        return _NO_MATCHES, []

    query, case_sensitive = _resolve_modifier(query)

    index_name = sql.Identifier(f"{table_name}_idx")
    token_index = sql.Identifier(f"{table_name}_token_idx")
    contains = f"%{query}%"
    prefix = f"{query}%"
    words = _words(query)

    logger.debug(
        "Auto-completing %r (words %s) in %s (case_sensitive=%s)",
        query,
        words,
        column,
        case_sensitive,
    )

    if column == "content":
        return _content_matches(index_name, contains, prefix, query, case_sensitive)

    def exact_branch() -> sql.Composable:
        return sql.SQL("""
            SELECT unnest(ts_dict_agg({column}))   AS suggestion,
                   unnest(ts_dict_count({column})) AS count
            FROM {index_name}
            WHERE {column} ILIKE %s
        """).format(column=sql.Identifier(column), index_name=index_name)

    def fuzzy_branch() -> sql.Composable:
        # Mistyping fallback: fuzzy-match each word against the tokenized index
        # and return the row's real value. ``lower`` on both sides (the terms are
        # lower-cased by the dictionary) makes it case-insensitive.
        terms = sql.SQL(", ").join(sql.SQL("ts_levenshtein(lower(%s))") for _ in words)
        return sql.SQL("""
            SELECT {column} AS suggestion, count(*) AS count
            FROM {token_index}
            WHERE {column} @@ ts_any([{terms}])
            GROUP BY {column}
        """).format(column=sql.Identifier(column), token_index=token_index, terms=terms)

    exact = exact_branch()
    fuzzy = fuzzy_branch()

    # ``ILIKE`` keeps the dictionary match case-insensitive; the ranking keys then
    # put values that start with the typed text ahead of values that merely
    # contain it, and -- only in case-sensitive mode -- values whose case matches
    # ahead of case-insensitive-only matches, before popularity.
    order_keys, order_params = _order_keys(case_sensitive, prefix, contains)
    order_by = sql.SQL("""
        ORDER BY {order_keys},
                 count DESC,
                 suggestion
        LIMIT {limit}
    """).format(order_keys=order_keys, limit=sql.Literal(_LIMIT))

    # The fuzzy rows are consulted only when the exact match came up empty, so a
    # normal keystroke never sees an edit-distance suggestion.
    combined = sql.SQL("""
        SELECT suggestion, count FROM exact_matches
        UNION ALL
        SELECT suggestion, count FROM fuzzy_matches
        WHERE NOT EXISTS (SELECT 1 FROM exact_matches)
    """)

    query_sql = sql.SQL("""
        WITH exact_matches AS ({exact}),
             fuzzy_matches AS ({fuzzy})
        SELECT suggestion, count
        FROM ({combined}) matches
        {order_by}
    """).format(exact=exact, fuzzy=fuzzy, combined=combined, order_by=order_by)

    # One ``%s`` for the exact ILIKE match, one per word for the fuzzy ts_any
    # branch, then the ORDER BY's ranking keys.
    params = [contains] + words + order_params
    return query_sql, params
