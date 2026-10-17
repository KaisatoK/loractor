"""Build and tear down the searchable OTel slice (Tier A).

The remote corpus cannot be searched: full-text search needs an inverted index,
and indexing a billion rows is neither affordable nor quick. The slice is the
answer -- a small, local, *fully* searchable extract of the part of the corpus
anyone actually investigates.

Two measured facts decide its shape:

* **A severity filter alone is not enough.** ``error`` is ~13.8% of the corpus,
  so problem-severity rows in one file number ~139 million. The slice must be
  severity-filtered **and** time-bounded.
* **Build time, not disk, is the binding constraint.** At 80 B/row the slice
  allowance would hold over 13M rows, but throughput degrades with size (16k
  rows/s at 156k rows, 6.3k at 687k), so a 13M-row build would take over half an
  hour. :func:`backend.db.budget.max_buildable_rows` caps by both.

The window is therefore **derived from the budget**, not configured. Given a row
ceiling and the measured arrival rate for the chosen severities, there is exactly
one window length that fills the slice without truncating it -- and a slice that
covers a complete contiguous window is honest in a way that a ``LIMIT``-truncated
one is not. A truncated slice silently covers only its first moments while
appearing to cover the whole requested range.

The slice is **materialized** rather than indexed as a view. A view-backed index
is 4x cheaper per row (73 B/row against 293), but materializing adds the
``line_id`` the raw corpus lacks, which is what keeps fetch-by-row and the UI's
``Line ID: n`` affordance working on OTel data. At slice scale the difference is
tens of megabytes against a 1 GiB allowance, so capability wins.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta

from psycopg2 import sql

from backend.db.budget import (
    BUDGET,
    CORPUS_FACTS,
    MATERIALIZED_BYTES_PER_ROW,
    MAX_SLICE_BUILD_SECONDS,
    PROBLEM_SEVERITIES,
    estimate_build_seconds,
    fits,
    max_buildable_rows,
)
from backend.db.datasets import (
    OTEL_SLICE_TABLE,
    OTEL_TEMPLATE_EXPRESSION,
    resolve,
)
from backend.db.errors import refuse

logger = logging.getLogger(__name__)

# Columns the slice carries. ``line_id`` is generated here because the source
# corpus has no primary key.
SLICE_COLUMNS = (
    "line_id",
    "Timestamp",
    "TraceId",
    "SpanId",
    "SeverityText",
    "ServiceName",
    "Body",
    "EventTemplate",
)

# Seconds of corpus time extracted per statement, when a rate is not known.
#
# A row group spans ~1.1 seconds, so one batch touches about one row group. This
# is the single most important safety parameter in the module: the crash that
# motivated batching came from one statement covering the slice's whole window,
# which had to hold its entire result in memory while streaming message text
# from object storage. Smaller batches trade a few more round trips for a hard
# bound on peak memory per statement.
BATCH_SECONDS = 1

# Rows one statement should aim to insert. Batches are sized by *rows* rather
# than by a fixed time span, because the two differ by two orders of magnitude
# across severity sets: problem severities arrive at ~78,000 rows/second while
# `fatal`+`warn` alone arrive at ~626. A fixed one-second batch would be right
# for the first and would need 2,875 statements for the second.
TARGET_BATCH_ROWS = 50_000

# Never span more than this in one statement, however sparse the severities.
MAX_BATCH_SECONDS = 60


def batch_seconds_for(severities: tuple[str, ...]) -> int:
    """Batch width that keeps one statement near :data:`TARGET_BATCH_ROWS`.

    Floored at one second because a row group spans ~1.1s: a narrower predicate
    cannot read less, so it would add round trips for nothing.
    """
    rate = arrival_rate(severities)
    if rate <= 0:
        return BATCH_SECONDS
    return max(BATCH_SECONDS, min(MAX_BATCH_SECONDS, int(TARGET_BATCH_ROWS / rate)))


@dataclass(frozen=True)
class SlicePlan:
    """What a slice build will do, before it does it."""

    severities: tuple[str, ...]
    start: datetime
    end: datetime
    estimated_rows: int
    estimated_bytes: int
    estimated_seconds: float

    @property
    def window_seconds(self) -> float:
        return (self.end - self.start) / timedelta(seconds=1)

    def as_dict(self) -> dict:
        return {
            "severities": list(self.severities),
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "window_seconds": round(self.window_seconds, 2),
            "estimated_rows": self.estimated_rows,
            "estimated_bytes": self.estimated_bytes,
            "estimated_build_seconds": round(self.estimated_seconds, 1),
        }


def arrival_rate(severities: tuple[str, ...]) -> float:
    """Rows per second carrying any of ``severities``, from measured fractions.

    part_000 holds 1e9 rows over ~1,764 seconds, so ~567k rows/second overall;
    the severity mix then scales that down.
    """
    rows_per_second = CORPUS_FACTS.rows_per_file / (
        CORPUS_FACTS.row_groups_per_file * CORPUS_FACTS.seconds_per_row_group
    )
    fraction = sum(
        CORPUS_FACTS.severity_distribution.get(s, 0.0) for s in severities
    )
    return rows_per_second * fraction


def plan_slice(
    severities: tuple[str, ...] = PROBLEM_SEVERITIES,
    *,
    start: str | None = None,
    max_rows: int | None = None,
    max_build_seconds: int = MAX_SLICE_BUILD_SECONDS,
) -> SlicePlan:
    """Derive the widest window that fits the budget without truncation.

    Refuses rather than truncating: a slice that quietly covers its first two
    seconds while claiming a thirty-second window would make every later answer
    wrong about its own coverage.
    """
    ceiling = max_rows or max_buildable_rows(
        BUDGET.otel_slice_allowance_bytes,
        max_build_seconds,
        materialized=True,
    )
    rate = arrival_rate(severities)
    if rate <= 0:
        raise refuse(
            f"Severities {severities} do not occur in the corpus.",
            "Use some of: " + ", ".join(CORPUS_FACTS.severity_distribution),
            code="unknown_severity",
        )

    seconds = ceiling / rate
    begin = datetime.fromisoformat(
        (start or f"{CORPUS_FACTS.corpus_day} 00:00:00+00").replace(" ", "T")
    )
    finish = begin + timedelta(seconds=seconds)
    estimated_rows = int(seconds * rate)

    return SlicePlan(
        severities=severities,
        start=begin,
        end=finish,
        estimated_rows=estimated_rows,
        estimated_bytes=estimated_rows * MATERIALIZED_BYTES_PER_ROW,
        estimated_seconds=estimate_build_seconds(estimated_rows),
    )


def _slice_cursor():
    """An autocommit cursor, so size probes between steps see real values.

    ``pg_database_size`` does not reflect uncommitted writes, so a build and its
    measurement inside one transaction would both read zero.
    """
    from backend.db.probe import probe_cursor

    return probe_cursor()


def _batches(plan: SlicePlan):
    """Yield ``(lo, hi)`` time bounds covering the plan in small steps.

    Half-open and contiguous, so every row is extracted exactly once. The step
    is sized from the plan's severities so each statement inserts roughly
    :data:`TARGET_BATCH_ROWS`.
    """
    step = timedelta(seconds=batch_seconds_for(plan.severities))
    lo = plan.start
    while lo < plan.end:
        hi = min(lo + step, plan.end)
        yield lo, hi
        lo = hi


def _create_table(cur) -> None:
    """Create the slice table explicitly.

    An explicit schema rather than ``CREATE TABLE AS`` because the rows arrive
    in batches, and because ``line_id`` has to exist as a real column the
    inserts can target -- the source corpus has no primary key to copy.
    """
    cur.execute(
        sql.SQL("""
            CREATE TABLE {t} (
                line_id BIGINT,
                "Timestamp" TIMESTAMPTZ,
                "TraceId" VARCHAR,
                "SpanId" VARCHAR,
                "SeverityText" VARCHAR,
                "ServiceName" VARCHAR,
                "Body" VARCHAR,
                "EventTemplate" VARCHAR
            )
        """).format(t=sql.Identifier(OTEL_SLICE_TABLE))
    )


def _db_bytes(cur) -> int:
    cur.execute("SELECT pg_database_size(current_database()) AS b")
    return int(cur.fetchone()["b"])


def build_slice(
    plan: SlicePlan | None = None,
    *,
    severities: tuple[str, ...] = PROBLEM_SEVERITIES,
    start: str | None = None,
    max_rows: int | None = None,
    dry_run: bool = False,
    progress=None,
) -> dict:
    """Materialize and index the slice, within budget, in small batches.

    The budget is checked **before** anything is built, against both the
    per-dataset allowance and the database's real current size. A refusal here
    costs nothing; discovering the problem mid-build costs a server.

    Index builds are the fragile operation on this host -- three crashes during
    development, the last one caused by a single statement that globally sorted
    its whole extract. So the extract is batched by time (one statement per
    :data:`BATCH_SECONDS`), ids are assigned with an unordered window function,
    no full-database ``VACUUM`` runs near a build, and sizes are measured rather
    than estimated.

    ``progress`` is an optional ``(batches, rows, through_timestamp)`` callback,
    so a long build can report as it goes.
    """
    plan = plan or plan_slice(severities, start=start, max_rows=max_rows)
    dataset = resolve("otel_s3")

    with _slice_cursor() as cur:
        verdict = fits(
            plan.estimated_bytes,
            dataset_id="otel_slice",
            current_db_bytes=_db_bytes(cur),
        )
        if not verdict.ok:
            raise refuse(
                verdict.reason,
                verdict.suggestion,
                code="over_budget",
                **{
                    "estimated_bytes": verdict.estimated_bytes,
                    "allowance_bytes": verdict.allowance_bytes,
                },
            )

        report: dict = {
            "plan": plan.as_dict(),
            "budget": verdict.reason,
            "dry_run": dry_run,
        }
        if dry_run:
            return report

        baseline = _db_bytes(cur)
        logger.info(
            "Building %s: %s rows over %.1fs from %s in %ds batches",
            OTEL_SLICE_TABLE,
            f"{plan.estimated_rows:,}",
            plan.window_seconds,
            plan.start.isoformat(),
            batch_seconds_for(plan.severities),
        )

        _drop(cur)
        _create_table(cur)

        severity_list = sql.SQL(", ").join(
            sql.Literal(s) for s in plan.severities
        )
        started = time.monotonic()
        rows = 0
        batches = 0

        # Built in small time-sliced batches, one statement per batch.
        #
        # This is the lesson of three server crashes: a single statement that
        # spans a wide time range has to hold its whole result while streaming
        # message text from object storage, and build cost is not composable.
        # One batch touches about one row group, so peak memory per statement is
        # bounded no matter how wide the slice's overall window is.
        for batch_lo, batch_hi in _batches(plan):
            cur.execute(
                sql.SQL("""
                    INSERT INTO {table} (
                        line_id, "Timestamp", "TraceId", "SpanId",
                        "SeverityText", "ServiceName", "Body", "EventTemplate"
                    )
                    SELECT
                        {offset} + row_number() OVER (),
                        "Timestamp", "TraceId", "SpanId",
                        "SeverityText", "ServiceName", "Body",
                        {template}
                    FROM {source}
                    WHERE "Timestamp" >= {lo}::timestamptz
                      AND "Timestamp" <  {hi}::timestamptz
                      AND "SeverityText" IN ({severities})
                """).format(
                    table=sql.Identifier(OTEL_SLICE_TABLE),
                    offset=sql.Literal(rows),
                    template=sql.SQL(OTEL_TEMPLATE_EXPRESSION),
                    source=dataset.scan_relation(),
                    lo=sql.Literal(batch_lo.isoformat(sep=" ")),
                    hi=sql.Literal(batch_hi.isoformat(sep=" ")),
                    severities=severity_list,
                )
            )
            # ``row_number() OVER ()`` carries no ORDER BY on purpose: ids only
            # need to be unique, and an ordered window would reintroduce the
            # global sort this batching exists to avoid.
            rows += max(cur.rowcount, 0)
            batches += 1
            if progress is not None:
                progress(batches, rows, batch_hi)

        report["extract_seconds"] = round(time.monotonic() - started, 2)
        report["batches"] = batches

        cur.execute(
            sql.SQL("SELECT count(*) AS n FROM {t}").format(
                t=sql.Identifier(OTEL_SLICE_TABLE)
            )
        )
        rows = int(cur.fetchone()["n"])
        report["rows"] = rows

        cur.execute(
            sql.SQL("VACUUM (REFRESH_TABLE) {t}").format(
                t=sql.Identifier(OTEL_SLICE_TABLE)
            )
        )
        after_table = _db_bytes(cur)

        _ensure_dictionaries(cur)

        started = time.monotonic()
        _build_indexes(cur)
        cur.execute(
            sql.SQL("VACUUM (REFRESH_TABLE) {t}").format(
                t=sql.Identifier(OTEL_SLICE_TABLE)
            )
        )
        report["index_seconds"] = round(time.monotonic() - started, 2)

        built = _db_bytes(cur)
        report["table_bytes"] = after_table - baseline
        report["index_bytes"] = built - after_table
        report["total_bytes"] = built - baseline
        report["bytes_per_row"] = round((built - baseline) / max(rows, 1), 1)
        report["within_allowance"] = (
            built - baseline
        ) <= BUDGET.otel_slice_allowance_bytes

        cur.execute(
            sql.SQL("""
                SELECT min("Timestamp") AS lo, max("Timestamp") AS hi
                FROM {t}
            """).format(t=sql.Identifier(OTEL_SLICE_TABLE))
        )
        bounds = cur.fetchone()
        report["covered_start"] = str(bounds["lo"])
        report["covered_end"] = str(bounds["hi"])

    logger.info("Slice built: %s", report)
    return report


def _ensure_dictionaries(cur) -> None:
    """Create the text search dictionaries the index needs.

    Same definitions the OpenStack setup uses, so ranking behaves identically
    across corpora. ``position`` and ``offset`` are what make phrase queries and
    highlighting possible.
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


def _build_indexes(cur) -> None:
    """The inverted index pair: verbatim values for type-ahead, words for search.

    ``ServiceName``, ``SeverityText`` and ``EventTemplate`` are indexed verbatim
    so the auto-complete recipe's ``ts_dict_agg`` returns their real values
    rather than lowercased tokens; ``Body`` keeps a dictionary for full-text
    search.
    """
    cur.execute(
        sql.SQL("""
            CREATE INDEX {idx} ON {t} USING inverted (
                line_id,
                "Body" english,
                "ServiceName",
                "SeverityText",
                "EventTemplate"
            )
        """).format(
            idx=sql.Identifier(f"{OTEL_SLICE_TABLE}_idx"),
            t=sql.Identifier(OTEL_SLICE_TABLE),
        )
    )
    cur.execute(
        sql.SQL("""
            CREATE INDEX {idx} ON {t} USING inverted (
                "ServiceName" dotted,
                "EventTemplate" english
            )
        """).format(
            idx=sql.Identifier(f"{OTEL_SLICE_TABLE}_token_idx"),
            t=sql.Identifier(OTEL_SLICE_TABLE),
        )
    )


def _drop(cur) -> None:
    cur.execute(
        sql.SQL("DROP TABLE IF EXISTS {t} CASCADE").format(
            t=sql.Identifier(OTEL_SLICE_TABLE)
        )
    )


def drop_slice() -> dict:
    """Drop the slice and report the disk reclaimed.

    Measured during development: ``DROP`` plus ``VACUUM`` returns
    ``pg_database_size`` exactly to its pre-build value, so this genuinely frees
    the allowance rather than leaking it.
    """
    with _slice_cursor() as cur:
        before = _db_bytes(cur)
        _drop(cur)
        # A full VACUUM is what actually reclaims the space. Safe here because
        # nothing is being built concurrently -- a bare VACUUM alongside a remote
        # index build is one of the things that took the server down.
        cur.execute("VACUUM")
        after = _db_bytes(cur)
    return {"freed_bytes": before - after, "db_bytes": after}


def refresh_slice() -> dict:
    """Rebuild the slice's indexes against the current table contents.

    The index is a snapshot; ``REINDEX`` runs one refresh pass and readers see
    either the old index or the new one, never half of each.
    """
    with _slice_cursor() as cur:
        started = time.monotonic()
        for suffix in ("_idx", "_token_idx"):
            cur.execute(
                sql.SQL("REINDEX INDEX {idx}").format(
                    idx=sql.Identifier(f"{OTEL_SLICE_TABLE}{suffix}")
                )
            )
        return {"reindex_seconds": round(time.monotonic() - started, 2)}


def slice_status() -> dict:
    """Whether the slice exists, how big it is, and what window it covers.

    The covered window matters as much as the row count: a search that finds
    nothing in the slice proves nothing about the hours the slice does not hold.
    """
    with _slice_cursor() as cur:
        cur.execute(
            "SELECT count(*) AS n FROM information_schema.tables "
            "WHERE table_name = %s",
            (OTEL_SLICE_TABLE,),
        )
        if not int(cur.fetchone()["n"]):
            return {"exists": False}

        cur.execute(
            sql.SQL("""
                SELECT count(*) AS rows,
                       min("Timestamp") AS lo,
                       max("Timestamp") AS hi,
                       count(DISTINCT "ServiceName") AS services
                FROM {t}
            """).format(t=sql.Identifier(OTEL_SLICE_TABLE))
        )
        row = cur.fetchone()
        cur.execute(
            sql.SQL("SELECT pg_total_relation_size({t}) AS b").format(
                t=sql.Literal(OTEL_SLICE_TABLE)
            )
        )
        size = int(cur.fetchone()["b"])

    return {
        "exists": True,
        "rows": int(row["rows"]),
        "covered_start": str(row["lo"]),
        "covered_end": str(row["hi"]),
        "services": int(row["services"]),
        "bytes": size,
        "allowance_bytes": BUDGET.otel_slice_allowance_bytes,
        "within_allowance": size <= BUDGET.otel_slice_allowance_bytes,
    }
