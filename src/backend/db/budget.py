"""Measured corpus facts and the enforced disk/RAM budget.

Everything in :data:`CORPUS_FACTS` was **measured** against the live corpus and
the live SereneDB on 2025-10-17 (SereneDB 26.10.1, PostgreSQL 18.3 wire). The
numbers supersede the estimates in the implementation plan; where a plan estimate
turned out wrong the discrepancy is called out in the comments so nobody
re-derives the old figure.

Measurement commands live in :mod:`backend.db.probe`; re-run them with
``uv run python -m backend.db.probe`` to refresh these constants.

Findings that shaped the design
-------------------------------

1. ``part_000`` holds exactly 1,000,000,000 rows in 1,624 row groups and is
   **38.2 GiB** on the wire. The 50-file corpus is therefore ~1.9 TB. Nothing
   about this fits locally, which is why the remote tier never materializes rows.

2. A row group is ~614,400 rows and spans only **~1.1 seconds** of telemetry.
   That is the unit of pruning: a time predicate narrower than a second cannot
   read less than one row group.

3. ``SeverityText`` is **lower case** -- ``info``, ``error``, ``information``,
   ``trace``, ``warn``, ``fatal``. The plan assumed ``'ERROR','FATAL','WARN'``;
   filtering on upper case silently matches nothing.

4. ``error`` is **13.84%** of all rows. The plan assumed a severity filter alone
   would carve out a slice small enough to index; it does not. part_000 holds
   ~138 million error rows, ~20x more than the index budget allows. The indexed
   slice is therefore severity-filtered **and** time-bounded, sized from
   :func:`max_indexable_rows`.

5. Scanning 17.5M rows with two projected columns took **2.63 s** (~6.6M rows/s)
   straight off S3. Column-pruned aggregates over the remote tier are cheap; it
   is reading ``Body`` that costs.

6. **A ``CREATE TABLE AS SELECT`` of 2.44M rows projecting ``Body`` OOM-killed
   the server** at ``memory_limit = 5.7 GiB``. A 100k-row materialization and a
   687k-row view-backed index build both completed cleanly, so the ceiling is
   somewhere between; :data:`PROMOTION_MAX_ROWS` sits an order of magnitude
   below the row count that crashed.

7. Making rows searchable through a **view plus inverted index costs 73-74
   B/row**, versus **293 B/row** to materialize them with ``CREATE TABLE AS
   SELECT`` (226 table + 67 index). The view path is 4x cheaper, and
   ``CREATE INDEX`` over a time-filtered view **does** prune row groups at build
   time -- a 2-second window indexed 156,150 rows in 9.8 s, which is impossible
   without pruning given the file is 38.2 GiB. The view path is therefore the
   default and the planned CTAS fallback is not needed.

8. **Index build throughput degrades with size**: 156k rows in 9.8 s (16k
   rows/s) but 687k rows in 109.3 s (6.3k rows/s). Disk is *not* the binding
   constraint on how much can be indexed -- time is. At 80 B/row a 1 GiB
   allowance would hold 13.4M rows, but building them would take over half an
   hour, so :func:`max_buildable_rows` caps by both.

10. **Remote index builds are the fragile operation on this host.** Two separate
    server crashes were caused by them, and the second is the informative one:
    a 687k-row view-backed build succeeded on its own, but a ~331k-row build
    crashed the server when it followed a 100k-row materialization and a full
    ``VACUUM`` in the same run. Build cost is therefore **not** composable --
    sequencing and concurrency matter as much as size. Three consequences, all
    load-bearing:

    * promotion is globally serialized (one build at a time, server-wide);
    * a full-database ``VACUUM`` is never issued near a build;
    * the probe's build measurements are opt-in, not part of the default run.

    Neither crash lost committed data: the ``log`` table and database size came
    back intact both times.

13. **A global window function over a remote extract is fatal.** The third
    crash came from a single ``CREATE TABLE AS SELECT row_number() OVER (ORDER
    BY "Timestamp") ... FROM read_parquet(...)``. The ordered window forces the
    engine to buffer and sort the entire result while streaming ``Body`` from
    object storage, which it cannot do even for a two-second window. Two rules
    follow, both implemented in :mod:`backend.db.slices`:

    * extract in **small time-sliced batches**, one statement each, sized by
      arrival rate so no statement handles much more than ~50,000 rows;
    * assign ids with ``row_number() OVER ()`` and **no ORDER BY** -- ids only
      need to be unique, and ordering reintroduces the global sort.

14. **The host has far less memory than the budget assumed, and it is
    shared.** Measured: 7,322 MB total, **~1,092 MB available**, swap
    essentially exhausted (2,044 of 2,048 MB used). The remainder is editor and
    agent tooling belonging to *two* different users, so SereneDB is one tenant
    among several rather than the machine's main occupant.

    This reframes all three crashes. ``memory_limit`` was 5.7 GiB on a host with
    ~1 GiB free, so SereneDB was authorised to request five times the memory the
    kernel could supply; what killed it was the OOM killer, not its own
    accounting. A memory ceiling above available RAM does not bound anything --
    it converts a recoverable "query exceeded memory" error into a dead server,
    and takes every other client down with it.

    Hence :attr:`Budget.memory_limit_bytes` is **512 MiB**, well under what is
    free, so operators spill to disk instead of being killed. Combined with
    batched extraction (finding 13) no single statement needs more. The user's
    stated 6 GB RAM allowance is simply not grantable on this host; 512 MiB is
    the honest figure.

15. **``threads`` is session-scoped, ``memory_limit`` is not.** Measured on the
    same server: after ``SET memory_limit`` and ``SET threads`` on one
    connection, a *fresh* connection reports the new ``memory_limit`` (488.2
    MiB) but the default ``threads`` (16). So the two settings that both look
    like ordinary GUCs behave oppositely -- one leaks globally and permanently,
    the other does not leak at all. Builds therefore lower ``threads`` per
    session (free, no effect on other clients) and treat ``memory_limit`` as a
    deliberate one-off operator action.

16. **Reading message text is the dominant cost of building anything, and it
    is paid per row group.** Measured on a one-second window (one row group):

    * a two-column aggregate over it: **1.43 s**;
    * the same window projecting ``Body``, ``LIMIT 5``: **33.3 s**;
    * the same window reading ``Body`` for all 72,678 problem-severity rows:
      **481.6 s** at 4 threads, **86.8 s** at 16.

    Two consequences. First, throttling threads is counterproductive (finding
    15's premise was wrong -- see :data:`SERENEDB_BUILD_THREADS`). Second, and
    more important for planning: the unit of build cost is the **row group
    touched**, not the row stored, because a row group's text column must be
    fetched and decompressed in full before any predicate can select from it.

    This inverts an earlier conclusion. Restricting the slice to rare severities
    looked like a bargain -- ``fatal``+``warn`` arrive at ~626 rows/s, so the
    disk allowance buys ~48 minutes of coverage against 23 seconds for all
    problem severities. But 48 minutes spans ~2,600 row groups, each costing a
    text read, so that slice would take **days** to build while the dense
    23-second one takes minutes. For slice building, *dense severities over a
    short contiguous window* is the affordable shape.

17. **Materialize first, then index: measured from a completed slice build.**
    150,557 problem-severity rows extracted from 2 row groups:

    * extract: **1,322.9 s** (~660 s per row group, cold);
    * index over the resulting *local* table: **0.71 s**;
    * table 4.19 MB, inverted index 19.12 MB, total **154.9 B/row**.

    Three corrections to earlier conclusions. The index is **4.5x larger than
    the table** it covers, because it inverts message text. Total cost per row
    is **154.9 B, not the 293 B** assumed from the CTAS probe, so the budget is
    conservative. And indexing a local table is effectively free (0.71 s against
    9.77 s for a comparable *view* over remote Parquet), because the view forced
    a second remote read at index time -- so the view path's 4x disk saving was
    paid for in repeated network cost.

    Also: warm measurements lie. The 86.8 s warm read of a row group's text
    underestimated the real cold build by 7.6x. Plan on cold figures.

11. **``statement_timeout`` through libpq ``options`` is silently ignored.** A
    connection opened with ``-c statement_timeout=30000`` reports ``0``, while
    ``SET LOCAL statement_timeout`` inside the transaction reports the value set.
    The pre-existing ``SERENEDB_STATEMENT_TIMEOUT_MS`` connection option was
    therefore never enforcing anything; the per-query-class ``SET LOCAL`` in
    :func:`backend.db.connection.query_runner` is what actually binds.

12. **``memory_limit`` is global and cannot be scoped.** ``SET LOCAL
    memory_limit`` fails outright (*option "memory_limit" cannot be set
    locally*), and a plain ``SET`` changes the ceiling for every client of the
    server and persists past the connection that issued it. Nothing in the
    connection path may touch it; see
    :func:`backend.db.connection.set_global_memory_limit`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

# --- Units -------------------------------------------------------------------
KIB = 1024
MIB = 1024 * KIB
GIB = 1024 * MIB


# --- Measured corpus facts ---------------------------------------------------
@dataclass(frozen=True)
class CorpusFacts:
    """Measured properties of one OTel Parquet part file.

    Measured against ``part_000.parquet``. The files are uniform by
    construction (one day of telemetry split into 50 equal parts), so these
    hold for any part; :mod:`backend.db.probe` re-measures per file when asked.
    """

    rows_per_file: int = 1_000_000_000
    """``parquet_file_metadata.num_rows`` for part_000: exactly 1e9."""

    row_groups_per_file: int = 1_624
    """``parquet_file_metadata.num_row_groups`` for part_000."""

    file_size_bytes: int = 40_991_166_725
    """``parquet_file_metadata.file_size_bytes`` -- 38.2 GiB compressed."""

    rows_per_row_group: int = 614_400
    """Modal ``row_group_num_rows``. A few groups carry 622,592."""

    seconds_per_row_group: float = 1.1
    """Measured from per-row-group ``Timestamp`` min/max stats."""

    corpus_files: int = 50
    """part_000 .. part_049, one day of telemetry."""

    corpus_day: str = "2025-09-23"
    """The UTC day the corpus covers, from row-group Timestamp stats."""

    remote_scan_rows_per_second: int = 6_600_000
    """17,463,418 rows aggregated in 2.63 s projecting 2 of 16 columns."""

    severity_distribution: dict[str, float] = field(
        default_factory=lambda: {
            "info": 0.6732,
            "error": 0.1384,
            "information": 0.1327,
            "trace": 0.0545,
            "warn": 0.0011,
            "fatal": 0.0000186,
        }
    )
    """Fractions measured over the first 30 s of part_000 (17,463,418 rows).

    Note the lower case, and note that ``error`` is a seventh of the corpus.
    """

    def compressed_bytes_per_row(self) -> float:
        """~41 bytes/row on the wire, across all 16 columns."""
        return self.file_size_bytes / self.rows_per_file

    def rows_in_severities(self, severities: tuple[str, ...]) -> int:
        """Estimated rows per file carrying any of ``severities``."""
        fraction = sum(self.severity_distribution.get(s, 0.0) for s in severities)
        return int(self.rows_per_file * fraction)


CORPUS_FACTS = CorpusFacts()

# Severity values that mark a row as worth indexing, in the corpus' own casing.
# ``warn`` and ``fatal`` are rare (0.11% and 0.002%); ``error`` dominates.
PROBLEM_SEVERITIES: tuple[str, ...] = ("error", "fatal", "warn")


# --- Index cost (MEASURED) ---------------------------------------------------
# Measured 2026-10-17 against part_000 on this machine. Two different costs,
# because there are two ways to make rows searchable and they differ by 4x:
#
#   view + inverted index   73.4 B/row and 74.1 B/row at 156k and 687k rows
#                           (postings plus INCLUDE columns; rows are NOT stored)
#   CTAS table + index      293.0 B/row at 100k rows
#                           (226.0 table + 66.9 index)
#
# The view path is both cheaper and correct -- build-time pruning holds, see
# probe.py -- so it is the default. The materialized figure stays because the
# OpenStack corpus and the BGL loader do store their rows.
INDEX_BYTES_PER_ROW: int = int(os.getenv("SERENEDB_INDEX_BYTES_PER_ROW", "80"))
"""Disk per searchable row for a view-backed inverted index. Measured 73-74."""

MATERIALIZED_BYTES_PER_ROW: int = int(
    os.getenv("SERENEDB_MATERIALIZED_BYTES_PER_ROW", "300")
)
"""Disk per row when rows are stored as well as indexed. Measured 293."""

# Index build throughput, measured on the remote corpus:
#
#   156,150 rows ->   9.77 s  (16,000 rows/s)
#   686,583 rows -> 109.31 s  ( 6,300 rows/s)
#
# Throughput DEGRADES with size, so the conservative figure is the one measured
# at the larger size. This is the constraint the plan missed: at 80 B/row the
# 1 GiB slice allowance would permit ~13.4M rows, but building that many would
# take over half an hour. Sizing must respect time as well as disk.
BUILD_ROWS_PER_SECOND: int = int(os.getenv("SERENEDB_BUILD_ROWS_PER_SECOND", "6000"))

MAX_PROMOTION_BUILD_SECONDS: int = 60
"""Wall-clock ceiling on building one promoted sweep chunk."""

MAX_SLICE_BUILD_SECONDS: int = 300
"""Wall-clock ceiling on building the standing searchable slice."""


@dataclass(frozen=True)
class Budget:
    """The hard capacity envelope SereneDB must stay inside on this machine.

    The user's cap is 3 GB disk and 6 GB RAM *total*, including the pre-existing
    OpenStack table. Allowances below sum to less than the cap on purpose:
    :data:`HEADROOM_BYTES` absorbs index write amplification during a build and
    the compaction tiers' transient segments.
    """

    total_disk_bytes: int = 3 * GIB
    """Hard ceiling. Nothing may be built that would take the database past it."""

    # 512 MiB, not the 3 GiB originally planned: the host has ~1 GiB free and is
    # shared with another user's tooling (finding 14). A ceiling above available
    # RAM turns a recoverable error into an OOM kill.
    memory_limit_bytes: int = 512 * MIB
    """Session ``memory_limit``.

    Deliberately below whatever the server is configured with -- the session
    limit exists to lower the ceiling, never to raise it. At 5.7 GiB a single
    2.44M-row materialization took the process down (finding 6); the server is
    now set to 3.8 GiB, and 3 GiB keeps a margin under that.
    """

    headroom_bytes: int = 512 * MIB
    """Reserved for build-time write amplification and compaction churn."""

    openstack_allowance_bytes: int = 64 * MIB
    """The existing demo table with its per-row embeddings (measured: 14.0 MiB)."""

    bgl_allowance_bytes: int = 384 * MIB
    """Local loghub BGL: inverted index plus template embeddings."""

    otel_slice_allowance_bytes: int = 1 * GIB
    """The standing severity-filtered + time-bounded OTel search slice."""

    promotion_allowance_bytes: int = 768 * MIB
    """One promoted sweep chunk at a time. Serialized, so this is not per-chunk
    multiplied -- it is the whole concurrent promotion footprint."""

    def allocated_bytes(self) -> int:
        return (
            self.openstack_allowance_bytes
            + self.bgl_allowance_bytes
            + self.otel_slice_allowance_bytes
            + self.promotion_allowance_bytes
            + self.headroom_bytes
        )

    def is_self_consistent(self) -> bool:
        """Whether the allowances plus headroom fit inside the disk cap."""
        return self.allocated_bytes() <= self.total_disk_bytes


BUDGET = Budget()

# Per-dataset allowance lookup, keyed by dataset id.
#
# ``otel_s3`` is the remote tier: it is read in place and may never build
# anything locally, so its allowance is zero and any build against it is refused
# by construction. ``otel_slice`` is the standing indexed slice, and
# ``otel_chunk`` is one promoted sweep chunk.
ALLOWANCES: dict[str, int] = {
    "openstack": BUDGET.openstack_allowance_bytes,
    "bgl": BUDGET.bgl_allowance_bytes,
    "otel_s3": 0,
    "otel_slice": BUDGET.otel_slice_allowance_bytes,
    "otel_chunk": BUDGET.promotion_allowance_bytes,
}


@dataclass(frozen=True)
class BudgetVerdict:
    """The outcome of a budget check, shaped for an agent-facing refusal."""

    ok: bool
    reason: str
    estimated_bytes: int
    allowance_bytes: int
    suggestion: str = ""

    def as_refusal(self) -> dict:
        """Render as the structured refusal the tool layer returns."""
        return {
            "status": "refused",
            "reason": self.reason,
            "suggestion": self.suggestion,
            "estimated_bytes": self.estimated_bytes,
            "allowance_bytes": self.allowance_bytes,
        }


def estimate_index_bytes(rows: int, *, materialized: bool = False) -> int:
    """Disk ``rows`` searchable rows are expected to occupy.

    ``materialized`` picks the 293 B/row figure for corpora that store their
    rows; the default 80 B/row is the view-backed index path.
    """
    per_row = MATERIALIZED_BYTES_PER_ROW if materialized else INDEX_BYTES_PER_ROW
    return int(rows * per_row)


def estimate_build_seconds(rows: int) -> float:
    """Wall-clock an index build over ``rows`` rows is expected to take.

    Linear in the measured worst-case throughput. The real curve is worse than
    linear, so this under-predicts large builds -- which is why
    :func:`max_buildable_rows` caps by time at all rather than trusting it.
    """
    return rows / BUILD_ROWS_PER_SECOND


def max_indexable_rows(allowance_bytes: int, *, materialized: bool = False) -> int:
    """How many rows fit in ``allowance_bytes`` of index. Disk only."""
    per_row = MATERIALIZED_BYTES_PER_ROW if materialized else INDEX_BYTES_PER_ROW
    if per_row <= 0:  # pragma: no cover - guarded by config
        raise ValueError("bytes-per-row must be positive")
    return allowance_bytes // per_row


def max_buildable_rows(
    allowance_bytes: int,
    max_seconds: int,
    *,
    materialized: bool = False,
) -> int:
    """How many rows may be made searchable, respecting disk **and** time.

    This is the function that sizes the indexed slice and the sweep chunk. Both
    limits are real and the binding one changes with configuration: at the
    measured 80 B/row and 6,000 rows/s, a 1 GiB allowance permits 13.4M rows on
    disk but only 1.8M in a 300-second build, so time binds. Sizing on disk
    alone -- as originally planned -- would have produced a slice that takes
    over half an hour to build.
    """
    return min(
        max_indexable_rows(allowance_bytes, materialized=materialized),
        int(max_seconds * BUILD_ROWS_PER_SECOND),
    )


def fits(
    estimated_bytes: int,
    *,
    dataset_id: str | None = None,
    current_db_bytes: int | None = None,
) -> BudgetVerdict:
    """Whether a build of ``estimated_bytes`` may proceed.

    Two independent checks, because either can be the binding one:

    * the per-dataset allowance, which stops one corpus from eating the others';
    * the total disk cap against the database's real current size, which stops
      the sum of several in-budget datasets from busting the envelope.

    ``current_db_bytes`` comes from ``pg_database_size``; pass ``None`` to skip
    the total check when the database is unreachable.
    """
    allowance = ALLOWANCES.get(dataset_id or "", BUDGET.promotion_allowance_bytes)

    if estimated_bytes > allowance:
        rows_that_fit = max_indexable_rows(allowance)
        return BudgetVerdict(
            ok=False,
            reason=(
                f"Estimated {_human(estimated_bytes)} exceeds the "
                f"{_human(allowance)} allowance for '{dataset_id}'."
            ),
            estimated_bytes=estimated_bytes,
            allowance_bytes=allowance,
            suggestion=(
                f"Narrow the request to at most ~{rows_that_fit:,} rows "
                "(tighten the time window or add a severity filter)."
            ),
        )

    if current_db_bytes is not None:
        projected = current_db_bytes + estimated_bytes
        if projected > BUDGET.total_disk_bytes:
            return BudgetVerdict(
                ok=False,
                reason=(
                    f"Database is {_human(current_db_bytes)}; adding "
                    f"{_human(estimated_bytes)} would reach "
                    f"{_human(projected)}, past the "
                    f"{_human(BUDGET.total_disk_bytes)} cap."
                ),
                estimated_bytes=estimated_bytes,
                allowance_bytes=allowance,
                suggestion=(
                    "Evict a promoted chunk or drop an unused dataset index "
                    "before building this one."
                ),
            )

    return BudgetVerdict(
        ok=True,
        reason=(
            f"Estimated {_human(estimated_bytes)} fits the "
            f"{_human(allowance)} allowance for '{dataset_id}'."
        ),
        estimated_bytes=estimated_bytes,
        allowance_bytes=allowance,
    )


def _human(num_bytes: int) -> str:
    """Byte count as a short human string, for refusal messages."""
    size = float(num_bytes)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(size) < 1024.0 or unit == "TiB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024.0
    return f"{size:.1f} TiB"  # pragma: no cover - unreachable


# --- Derived sizing ----------------------------------------------------------
# Row counts for the searchable slice and one promoted chunk. Both derived from
# the measured per-row cost AND the measured build throughput, never hardcoded,
# so re-measuring either resizes them consistently.
OTEL_SLICE_MAX_ROWS: int = max_buildable_rows(
    BUDGET.otel_slice_allowance_bytes, MAX_SLICE_BUILD_SECONDS
)

CHUNK_TARGET_ROWS: int = max_buildable_rows(
    BUDGET.promotion_allowance_bytes, MAX_PROMOTION_BUILD_SECONDS
)

# Hard row ceiling on one promotion, independent of the byte and time math.
#
# A 2.44M-row materialization OOM-killed the server during measurement. A 100k
# materialization and a 687k view-backed build both completed cleanly. The
# ceiling sits an order of magnitude below the row count that crashed, and the
# byte and time budgets only ever lower it further.
PROMOTION_MAX_ROWS: int = 250_000

# Rows per sweep chunk: the smallest of the three limits.
CHUNK_ROWS: int = min(CHUNK_TARGET_ROWS, PROMOTION_MAX_ROWS)
