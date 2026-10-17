"""Corpus arithmetic and the shared text dictionaries.

These were originally inside the materialized-slice builder, which is where they
were first needed. The slice is gone -- the corpus is indexed in place now --
but the arithmetic outlived it, because *any* operation that reads message text
pays the same costs: the external index builder and the sweep's chunk promotion
both need to know how many row groups a window touches and how fast rows of a
given severity arrive.

Keeping them here rather than in :mod:`backend.db.budget` because they describe
the *corpus*, not the machine's limits, and because the text search
dictionaries belong beside them: every index in the system has to tokenize the
same way or BM25 scores stop being comparable between corpora.
"""

from __future__ import annotations

import math

from backend.db.budget import CORPUS_FACTS

# Seconds to fetch and decompress one row group's message-text column, cold, by
# way of a plain SELECT.
#
# Measured from a completed slice build: 1,322.9 s of extract across 2 row
# groups. An earlier 86.8 s reading of the *same* window was cache-warmed and
# badly misleading -- planning on it underestimated a real build by 7.6x. This
# is the cold figure, the only honest one for a corpus too large to cache.
#
# Note this is the cost of *reading* text. Building an external index over the
# same text is far cheaper and does not scale per row group at all; see
# :mod:`backend.db.external`.
BODY_SECONDS_PER_ROW_GROUP = 660.0


def row_groups_touched(window_seconds: float) -> int:
    """Row groups a window overlaps, from the measured ~1.1 s span of each.

    The unit of cost for anything that reads message text: a row group's column
    chunk must be fetched and decompressed in full before any predicate can
    select from it, so a window narrower than one row group costs the same as
    one.
    """
    return max(
        1, math.ceil(window_seconds / CORPUS_FACTS.seconds_per_row_group)
    )


def estimate_text_read_seconds(window_seconds: float) -> float:
    """Wall-clock to read message text across a window with a plain SELECT."""
    return row_groups_touched(window_seconds) * BODY_SECONDS_PER_ROW_GROUP


def arrival_rate(severities: tuple[str, ...]) -> float:
    """Rows per second of corpus time carrying any of ``severities``.

    Derived from the measured severity mix rather than sampled, so it is stable:
    part_000 holds 1e9 rows over ~1,764 seconds, about 567k rows/second overall,
    which the mix then scales down. Problem severities arrive at ~78,000 rows/s;
    ``fatal``+``warn`` alone at ~626.
    """
    rows_per_second = CORPUS_FACTS.rows_per_file / (
        CORPUS_FACTS.row_groups_per_file * CORPUS_FACTS.seconds_per_row_group
    )
    fraction = sum(
        CORPUS_FACTS.severity_distribution.get(s, 0.0) for s in severities
    )
    return rows_per_second * fraction


def ensure_dictionaries(cur) -> None:
    """Create the text search dictionaries every index in the system shares.

    Shared deliberately: BM25 scores are only comparable across corpora if the
    same tokenization produced them, so OpenStack, BGL and the OTel index all
    attach the same ``english`` dictionary to their message column.

    ``position`` and ``offset`` are what make phrase queries and highlighting
    possible; ``dotted`` splits on non-alphanumerics for the auto-complete
    recipe's near-miss fallback.
    """
    cur.execute("""
        CREATE TEXT SEARCH DICTIONARY IF NOT EXISTS english AS
        split_text(case := 'lower') WITH (frequency, position, offset)
    """)
    cur.execute("""
        CREATE TEXT SEARCH DICTIONARY IF NOT EXISTS dotted AS
        split_by_non_alpha() | normalize_tokens('en_US.UTF-8', case := 'lower')
        WITH (frequency, position)
    """)
