"""Promote one sweep chunk to searchable, summarise it, then give the disk back.

This is the expensive half of the divide-and-conquer sweep. Stages 1 and 2
(:mod:`backend.db.profile`) read no message text and so can look at all 1,624
chunks cheaply. Promotion reads text, and text is what costs:

    a chunk is one row group, and one row group's text column takes ~660 s
    to fetch and decompress

So promotion is measured in **minutes per chunk**, which dictates everything
here. It is strictly serialized, it is capped per investigation, its cost is
reported before it is spent, and -- most importantly -- what it produces is a
**digest** rather than a retained table.

The digest is the point. A promoted chunk occupies ~39 MiB and cannot be kept
for 1,624 chunks, but its digest is a few kilobytes: counts, the templates that
dominated it, verbatim exemplar lines with their timestamps, and the triage
score that justified promoting it. Digests accumulate across a whole sweep and
survive the chunks they came from, which is what lets an investigation cover far
more of the corpus than it could ever hold at once.

Disk is returned, not leaked: a measured ``DROP`` plus ``VACUUM`` restores
``pg_database_size`` exactly to its pre-promotion value, and
:func:`promote_chunk` verifies that rather than assuming it.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime

from psycopg2 import sql

from backend.db.budget import (
    ALLOWANCES,
    PROBLEM_SEVERITIES,
    estimate_index_bytes,
    fits,
)
from backend.db.chunks import Chunk
from backend.db.datasets import OTEL_TEMPLATE_EXPRESSION, Dataset, resolve
from backend.db.errors import refuse
from backend.db.slices import (
    BODY_SECONDS_PER_ROW_GROUP,
    _ensure_dictionaries,
)

logger = logging.getLogger(__name__)

# Only one promotion may run at a time, process-wide.
#
# Not a performance choice. Two index builds that each succeed alone took the
# server down when they overlapped (budget.py finding 11): build cost is not
# composable, and the failure mode is a dead server rather than a slow query.
# Serializing also makes the disk ceiling trivially true -- at most one
# promotion's bytes exist at any moment.
_PROMOTION_LOCK = threading.Lock()

# Exemplar lines kept per digest. Enough to show a pattern and to quote
# verbatim; small enough that 1,624 digests stay in memory.
EXEMPLARS_PER_DIGEST = 5

# Templates kept per digest, ranked by how many rows they account for.
TEMPLATES_PER_DIGEST = 10

# Longest a single promotion may take before it is abandoned. Two row groups'
# worth of text read, so a chunk that is unusually slow does not consume an
# investigation's whole time budget.
MAX_PROMOTION_SECONDS = int(BODY_SECONDS_PER_ROW_GROUP * 2)


@dataclass(frozen=True)
class ChunkDigest:
    """What is kept after a promoted chunk is discarded.

    Deliberately includes ``exemplars`` verbatim. An aggregate says a chunk held
    12,000 card-charge failures; only a real line shows the error code, the
    service that emitted it and the shape of the message. Evidence that cannot
    be quoted is not much use in an investigation.
    """

    chunk_id: str
    rows_indexed: int
    rows_scanned: int
    ts_lo: datetime
    ts_hi: datetime
    severity_counts: dict[str, int]
    service_counts: dict[str, int]
    top_templates: tuple[tuple[str, int], ...]
    exemplars: tuple[dict, ...]
    triage_score: float | None = None
    build_seconds: float = 0.0
    bytes_used: int = 0
    notes: tuple[str, ...] = field(default=())

    @property
    def problem_rows(self) -> int:
        return sum(
            self.severity_counts.get(severity, 0)
            for severity in PROBLEM_SEVERITIES
        )

    def as_dict(self) -> dict:
        """Serializable form. Carries no URLs, matching the chunk map."""
        return {
            "chunk_id": self.chunk_id,
            "rows_indexed": self.rows_indexed,
            "rows_scanned": self.rows_scanned,
            "time_range": [self.ts_lo.isoformat(), self.ts_hi.isoformat()],
            "severity_counts": dict(self.severity_counts),
            "service_counts": dict(self.service_counts),
            "top_templates": [
                {"template": text, "rows": count}
                for text, count in self.top_templates
            ],
            "exemplars": [dict(e) for e in self.exemplars],
            "problem_rows": self.problem_rows,
            "triage_score": self.triage_score,
            "build_seconds": round(self.build_seconds, 1),
            "bytes_used": self.bytes_used,
            "notes": list(self.notes),
        }


def promoted_table(chunk_id: str) -> str:
    """Table name for a promoted chunk.

    The chunk id (``part_000:12-12``) carries a colon and a hyphen, so it is
    sanitised rather than quoted -- a predictable name is easier to find and
    drop by hand if a promotion is interrupted.
    """
    return "otel_promoted_" + re.sub(r"[^0-9a-zA-Z]+", "_", chunk_id)


def estimate_promotion_seconds(chunk: Chunk) -> float:
    """Wall-clock a promotion will cost, from the measured text-read rate.

    This is the number worth showing a user before starting: at ~660 s per row
    group, four promotions are a three-quarter-hour commitment, and that is
    worth confirming rather than discovering.
    """
    return max(1, chunk.row_groups) * BODY_SECONDS_PER_ROW_GROUP


def _promotion_cursor():
    from backend.db.probe import probe_cursor

    return probe_cursor()


def _db_bytes(cur) -> int:
    cur.execute("SELECT pg_database_size(current_database()) AS b")
    return int(cur.fetchone()["b"])


def _drop_promoted(cur, table: str) -> None:
    cur.execute(
        sql.SQL("DROP TABLE IF EXISTS {t} CASCADE").format(t=sql.Identifier(table))
    )


def promote_chunk(
    chunk: Chunk,
    *,
    dataset: Dataset | str = "otel_s3",
    severities: tuple[str, ...] = PROBLEM_SEVERITIES,
    question: str = "",
    triage_score: float | None = None,
    keep: bool = False,
) -> ChunkDigest:
    """Materialize a chunk's problem rows, summarise them, then drop them.

    One chunk, one lock, one budget check, and a digest at the end. ``keep``
    leaves the table in place for follow-up queries, which the sweep does not
    use -- it exists so an interactive caller can look at a chunk it just paid
    for without paying again.
    """
    ds = resolve(dataset) if isinstance(dataset, str) else dataset
    table = promoted_table(chunk.chunk_id)

    # Serialized process-wide: concurrent builds are what killed the server.
    if not _PROMOTION_LOCK.acquire(blocking=False):
        raise refuse(
            "Another chunk is being promoted.",
            "Promotions run one at a time; retry when the current one finishes.",
            code="promotion_busy",
            chunk_id=chunk.chunk_id,
        )

    try:
        with _promotion_cursor() as cur:
            baseline = _db_bytes(cur)

            # Rows surviving the severity filter, not rows read. The filter cuts
            # a row group by roughly 7x, which is what makes a chunk indexable
            # at all.
            expected_rows = int(chunk.num_rows * 0.15)
            verdict = fits(
                estimate_index_bytes(expected_rows, materialized=True),
                dataset_id="otel_chunk",
                current_db_bytes=baseline,
            )
            if not verdict.ok:
                raise refuse(
                    verdict.reason,
                    verdict.suggestion,
                    code="over_budget",
                    chunk_id=chunk.chunk_id,
                )

            logger.info(
                "Promoting %s (~%d rows, est %.0fs)",
                chunk.chunk_id,
                expected_rows,
                estimate_promotion_seconds(chunk),
            )

            started = time.monotonic()
            _drop_promoted(cur, table)
            _materialize(cur, ds, chunk, table, severities)
            build_seconds = time.monotonic() - started

            digest = _summarise(
                cur,
                table,
                chunk,
                triage_score=triage_score,
                build_seconds=build_seconds,
            )

            if not keep:
                _drop_promoted(cur, table)
                cur.execute("VACUUM")
                returned = _db_bytes(cur)
                # The leak check, run rather than assumed. Measured behaviour is
                # that DROP + VACUUM returns pg_database_size exactly to
                # baseline; a sweep over hundreds of chunks depends on it.
                leaked = returned - baseline
                if leaked > 1024 * 1024:
                    logger.warning(
                        "Promotion of %s left %d bytes behind",
                        chunk.chunk_id,
                        leaked,
                    )
                    digest = replace_notes(
                        digest, f"teardown left {leaked:,} bytes on disk"
                    )

        return digest
    finally:
        _PROMOTION_LOCK.release()


def replace_notes(digest: ChunkDigest, note: str) -> ChunkDigest:
    """A digest with one more note. Frozen, so this returns a new one."""
    from dataclasses import replace

    return replace(digest, notes=(*digest.notes, note))


def _materialize(
    cur,
    dataset: Dataset,
    chunk: Chunk,
    table: str,
    severities: tuple[str, ...],
) -> None:
    """Copy the chunk's problem rows into a local table, then index them.

    One statement for the extract, because a chunk is a single row group and so
    is already the smallest unit the remote corpus can be read in -- the slice
    builder's batching exists to break up *wider* windows and has nothing to
    subdivide here.

    Indexing happens after materializing, never over a view of the remote file:
    measured, a local index build takes 0.71 s where the view-backed equivalent
    took 9.77 s because it re-read the remote data.
    """
    cur.execute(
        sql.SQL("""
            CREATE TABLE {t} AS
            SELECT
                row_number() OVER () AS line_id,
                "Timestamp", "TraceId", "SeverityText", "ServiceName", "Body",
                {template} AS "EventTemplate"
            FROM {source}
            WHERE "Timestamp" >= {lo}::timestamptz
              AND "Timestamp" <  {hi}::timestamptz
              AND "SeverityText" IN ({severities})
        """).format(
            t=sql.Identifier(table),
            template=sql.SQL(OTEL_TEMPLATE_EXPRESSION),
            source=dataset.scan_relation(),
            lo=sql.Literal(chunk.ts_lo.isoformat(sep=" ")),
            hi=sql.Literal(chunk.ts_hi.isoformat(sep=" ")),
            severities=sql.SQL(", ").join(sql.Literal(s) for s in severities),
        )
    )
    cur.execute(
        sql.SQL("VACUUM (REFRESH_TABLE) {t}").format(t=sql.Identifier(table))
    )
    _ensure_dictionaries(cur)
    cur.execute(
        sql.SQL("""
            CREATE INDEX {idx} ON {t} USING inverted (
                line_id, "Body" english, "ServiceName", "SeverityText",
                "EventTemplate"
            )
        """).format(idx=sql.Identifier(f"{table}_idx"), t=sql.Identifier(table))
    )
    cur.execute(
        sql.SQL("VACUUM (REFRESH_TABLE) {t}").format(t=sql.Identifier(table))
    )


def _summarise(
    cur,
    table: str,
    chunk: Chunk,
    *,
    triage_score: float | None,
    build_seconds: float,
) -> ChunkDigest:
    """Reduce a promoted chunk to a digest, in four small queries.

    Aggregates first, exemplars last. The exemplars are chosen by severity and
    then by time so they are reproducible: a sweep re-run on the same chunk
    quotes the same lines, which matters when a digest is cited as evidence.
    """
    cur.execute(
        sql.SQL("SELECT count(*) AS n FROM {t}").format(t=sql.Identifier(table))
    )
    rows_indexed = int(cur.fetchone()["n"])

    cur.execute(
        sql.SQL("""
            SELECT "SeverityText" AS k, count(*) AS n FROM {t} GROUP BY 1
        """).format(t=sql.Identifier(table))
    )
    severity_counts = {r["k"]: int(r["n"]) for r in cur.fetchall()}

    cur.execute(
        sql.SQL("""
            SELECT "ServiceName" AS k, count(*) AS n FROM {t}
            GROUP BY 1 ORDER BY n DESC LIMIT 20
        """).format(t=sql.Identifier(table))
    )
    service_counts = {r["k"]: int(r["n"]) for r in cur.fetchall()}

    cur.execute(
        sql.SQL("""
            SELECT "EventTemplate" AS t, count(*) AS n FROM {t}
            GROUP BY 1 ORDER BY n DESC, t LIMIT {limit}
        """).format(
            t=sql.Identifier(table), limit=sql.Literal(TEMPLATES_PER_DIGEST)
        )
    )
    top_templates = tuple((r["t"], int(r["n"])) for r in cur.fetchall())

    # Worst severity first, then earliest -- the first sign of a problem is more
    # useful than an arbitrary sample of it.
    cur.execute(
        sql.SQL("""
            SELECT "Timestamp" AS ts, "SeverityText" AS severity,
                   "ServiceName" AS service, "Body" AS message,
                   "TraceId" AS trace_id
            FROM {t}
            ORDER BY CASE "SeverityText"
                        WHEN 'fatal' THEN 0 WHEN 'error' THEN 1
                        WHEN 'warn' THEN 2 ELSE 3 END,
                     "Timestamp"
            LIMIT {limit}
        """).format(
            t=sql.Identifier(table), limit=sql.Literal(EXEMPLARS_PER_DIGEST)
        )
    )
    exemplars = tuple(
        {
            "file": chunk.file_name,
            "chunk_id": chunk.chunk_id,
            "ts": str(r["ts"]),
            "severity": r["severity"],
            "service": r["service"],
            "trace_id": r["trace_id"],
            "message": r["message"],
        }
        for r in cur.fetchall()
    )

    cur.execute(
        sql.SQL("SELECT pg_total_relation_size({t}) AS b").format(
            t=sql.Literal(table)
        )
    )
    bytes_used = int(cur.fetchone()["b"])

    return ChunkDigest(
        chunk_id=chunk.chunk_id,
        rows_indexed=rows_indexed,
        rows_scanned=chunk.num_rows,
        ts_lo=chunk.ts_lo,
        ts_hi=chunk.ts_hi,
        severity_counts=severity_counts,
        service_counts=service_counts,
        top_templates=top_templates,
        exemplars=exemplars,
        triage_score=triage_score,
        build_seconds=build_seconds,
        bytes_used=bytes_used,
    )


def drop_all_promoted() -> dict:
    """Remove every promoted chunk table and report the disk reclaimed.

    A safety net for an interrupted sweep: promotion names its tables
    predictably so they can be found and dropped without knowing which chunks
    were in flight.
    """
    with _promotion_cursor() as cur:
        before = _db_bytes(cur)
        cur.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_name LIKE 'otel_promoted_%'"
        )
        tables = [r["table_name"] for r in cur.fetchall()]
        for table in tables:
            _drop_promoted(cur, table)
        if tables:
            cur.execute("VACUUM")
        after = _db_bytes(cur)

    return {
        "dropped": tables,
        "freed_bytes": before - after,
        "db_bytes": after,
    }


def promotion_capacity() -> dict:
    """How many chunks could be promoted at once, and what one costs.

    Serialization means the answer is always one; this reports the arithmetic
    behind that so the limit does not look arbitrary.
    """
    per_chunk = estimate_index_bytes(int(614_400 * 0.15), materialized=True)
    return {
        "allowance_bytes": ALLOWANCES["otel_chunk"],
        "estimated_bytes_per_chunk": per_chunk,
        "concurrent_promotions": 1,
        "estimated_seconds_per_chunk": BODY_SECONDS_PER_ROW_GROUP,
        "note": (
            "Promotions are serialized: concurrent index builds crashed the "
            "server during development, and the cost of one is dominated by "
            "reading message text, not by disk."
        ),
    }
