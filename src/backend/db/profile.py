"""Sweep stages 1 and 2: profile every chunk cheaply, then sample a few.

The funnel's economy lives here.

**Stage 1 -- coarse profile.** *One* grouped aggregate over the timestamp and
severity columns profiles *every* chunk at once. Not one query per chunk: at
1,624 chunks per file that would be 1,624 round trips. Two of sixteen columns are
projected and no ``Body`` byte is read, so this is the cheapest possible way to
learn where the errors are.

**Stage 2 -- stratified sampling.** Only for chunks Stage 1 ranked highly. S
evenly spaced micro-windows inside the chunk, each with a tight ``LIMIT``, so the
sample spans the chunk's whole time range instead of clustering at its start.

Stratified micro-windows are used in preference to ``USING SAMPLE`` (which this
server does support) precisely because of that spread: a uniform sample of a
chunk tells you what is common, while a time-spread sample tells you whether
something changed *during* the chunk, which is what an investigation is usually
asking.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta

from psycopg2 import sql

from backend.db import predicates as pred
from backend.db.chunks import Chunk
from backend.db.connection import bulk_query
from backend.db.datasets import Dataset, resolve
from backend.db.stats import QueryStats
from backend.db.windows import TimeWindow, build_window

logger = logging.getLogger(__name__)

# Micro-windows per sampled chunk, and rows per micro-window. 8 x 250 = 2,000
# rows per chunk: enough to see which templates and services dominate, small
# enough that sampling a hundred chunks is still cheaper than promoting one.
SAMPLE_WINDOWS = 8
SAMPLE_ROWS_PER_WINDOW = 250

MAX_SAMPLE_ROWS = SAMPLE_WINDOWS * SAMPLE_ROWS_PER_WINDOW


@dataclass
class ChunkProfile:
    """What Stage 1 learned about one chunk. No message text was read."""

    chunk_id: str
    rows: int
    severity_counts: dict[str, int] = field(default_factory=dict)
    service_counts: dict[str, int] = field(default_factory=dict)

    @property
    def total_counted(self) -> int:
        return sum(self.severity_counts.values())

    def error_rate(self, problem: tuple[str, ...]) -> float:
        """Fraction of counted rows carrying a problem severity."""
        total = self.total_counted
        if not total:
            return 0.0
        return sum(self.severity_counts.get(s, 0) for s in problem) / total

    def error_rows(self, problem: tuple[str, ...]) -> int:
        return sum(self.severity_counts.get(s, 0) for s in problem)

    def as_dict(self) -> dict:
        return {
            "chunk_id": self.chunk_id,
            "rows": self.rows,
            "severity_counts": dict(self.severity_counts),
            "top_services": dict(
                sorted(self.service_counts.items(), key=lambda kv: -kv[1])[:5]
            ),
        }


@bulk_query
def profile_buckets(
    start_time: str,
    end_time: str,
    dataset: str | None = None,
    bucket: str = "second",
    by_service: bool = False,
) -> tuple[sql.Composable, list, QueryStats]:
    """Stage 1: severity counts per time bucket across the whole window.

    One query for every chunk in the window. ``bucket='second'`` matches this
    corpus' ~1.1-second row groups closely enough to attribute each bucket to a
    chunk; a coarser bucket costs the same to run but resolves fewer chunks.

    ``by_service`` adds the service dimension. It is off by default because it
    multiplies the returned rows by the service count for information that only
    matters once a chunk is already a candidate.
    """
    from backend.db.pruning import plan_and_guard

    ds = resolve(dataset)
    window = build_window(ds, start_time, end_time)
    stats = plan_and_guard(ds, window)

    bucket_sql = pred.bucket_expression(ds, bucket)
    columns = [sql.SQL("{b} AS bucket").format(b=bucket_sql)]
    group = [bucket_sql]

    if ds.has_field("severity"):
        severity = ds.sql_for("severity")
        columns.append(sql.SQL("{c} AS severity").format(c=severity))
        group.append(severity)

    if by_service and ds.has_field("service"):
        service = ds.sql_for("service")
        columns.append(sql.SQL("{c} AS service").format(c=service))
        group.append(service)

    clause, params = pred.where(pred.ts_filter(ds, window))

    query = sql.SQL("""
        SELECT {columns}, count(*) AS count
        FROM {relation}
        {clause}
        GROUP BY {group}
        ORDER BY bucket
    """).format(
        columns=sql.SQL(", ").join(columns),
        relation=ds.scan_relation(),
        clause=clause,
        group=sql.SQL(", ").join(group),
    )
    stats.notes.append(f"coarse profile, {bucket} buckets, no message text read")
    return query, params, stats


def attach_profiles(chunks: tuple[Chunk, ...], rows: list[dict]) -> list[ChunkProfile]:
    """Map Stage 1's bucket rows onto chunks. Pure, so it is testable offline.

    A bucket belongs to the chunk whose half-open time range contains it. Buckets
    outside every chunk are dropped rather than guessed at.
    """
    from backend.db.pruning import _to_datetime

    profiles = {c.chunk_id: ChunkProfile(c.chunk_id, c.num_rows) for c in chunks}
    ordered = sorted(chunks, key=lambda c: c.ts_lo)

    for row in rows:
        moment = _to_datetime(row["bucket"])
        chunk = _locate(ordered, moment)
        if chunk is None:
            continue
        profile = profiles[chunk.chunk_id]
        count = int(row["count"])
        severity = row.get("severity")
        if severity is not None:
            profile.severity_counts[str(severity)] = (
                profile.severity_counts.get(str(severity), 0) + count
            )
        service = row.get("service")
        if service is not None:
            profile.service_counts[str(service)] = (
                profile.service_counts.get(str(service), 0) + count
            )

    return [profiles[c.chunk_id] for c in ordered]


def _locate(ordered: list[Chunk], moment) -> Chunk | None:
    """Binary search for the chunk containing ``moment``."""
    lo, hi = 0, len(ordered) - 1
    while lo <= hi:
        mid = (lo + hi) // 2
        chunk = ordered[mid]
        if moment < chunk.ts_lo:
            hi = mid - 1
        elif moment >= chunk.ts_hi:
            lo = mid + 1
        else:
            return chunk
    return None


def micro_windows(chunk: Chunk, count: int = SAMPLE_WINDOWS) -> list[TimeWindow]:
    """``count`` evenly spaced sub-windows spanning the chunk's time range."""
    span = chunk.ts_hi - chunk.ts_lo
    if count < 1 or span <= timedelta(0):
        return []

    step = span / count
    windows: list[TimeWindow] = []
    for index in range(count):
        lo = chunk.ts_lo + step * index
        hi = chunk.ts_lo + step * (index + 1) if index + 1 < count else chunk.ts_hi
        windows.append(
            TimeWindow(
                start=lo.isoformat(sep=" "),
                end=hi.isoformat(sep=" "),
                seconds=(hi - lo) / timedelta(seconds=1),
            )
        )
    return windows


@bulk_query
def sample_chunk(
    chunk: Chunk,
    dataset: str | None = None,
    severity: str | None = None,
    windows: int = SAMPLE_WINDOWS,
    rows_per_window: int = SAMPLE_ROWS_PER_WINDOW,
) -> tuple[sql.Composable, list, QueryStats]:
    """Stage 2: a time-spread sample of one chunk, with message text.

    Each micro-window is its own ``LIMIT``-ed subquery, UNION ALL-ed together, so
    the row cap applies per window and the sample cannot all come from the
    chunk's first instant.

    This is the first stage that reads ``Body``, and it reads at most
    ``windows * rows_per_window`` of them.
    """
    ds = resolve(dataset)
    slices = micro_windows(chunk, windows)
    if not slices:
        from backend.db.errors import refuse

        raise refuse(
            f"Chunk '{chunk.chunk_id}' has no time span to sample.",
            "Pick a different chunk from the ranked list.",
            code="empty_chunk",
        )

    wanted = [
        name
        for name in ("ts", "severity", "service", "message", "template")
        if ds.has_field(name)
    ]
    columns = sql.SQL(", ").join(
        sql.SQL("{col} AS {alias}").format(
            col=ds.sql_for(name), alias=sql.Identifier(name)
        )
        for name in wanted
    )

    branches: list[sql.Composable] = []
    params: list = []
    severity_clause, severity_params = pred.severity_filter(ds, severity)

    for window in slices:
        clause, clause_params = pred.where(
            pred.ts_filter(ds, window), (severity_clause, severity_params)
        )
        branches.append(
            sql.SQL("""
                (SELECT {columns} FROM {relation} {clause} LIMIT {n})
            """).format(
                columns=columns,
                relation=ds.scan_relation(),
                clause=clause,
                n=sql.Literal(rows_per_window),
            )
        )
        params.extend(clause_params)

    query = sql.SQL(" UNION ALL ").join(branches)

    stats = QueryStats(
        dataset=ds.id,
        approximate=True,
        row_groups_read=chunk.row_groups,
        row_groups_considered=chunk.row_groups,
        est_rows_scanned=chunk.num_rows,
        est_bytes_read=chunk.compressed_bytes,
        window_seconds=chunk.seconds,
    )
    stats.notes.append(
        f"stratified sample: {len(slices)} windows x {rows_per_window} rows "
        f"from chunk {chunk.chunk_id}"
    )
    return query, params, stats
