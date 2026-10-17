"""The chunk map: addressing a billion-row file in pieces, for free.

A chunk is a run of **contiguous row groups**. Because the corpus is
timestamp-sorted and every row group carries ``stats_min_value`` /
``stats_max_value`` for the timestamp column, a chunk can be addressed purely as
``ts >= lo AND ts < hi`` -- a predicate that maps exactly onto row-group
boundaries and lets zonemaps skip everything else.

That is the whole trick, and it is why the sweep needs no paging: chunk identity
comes out of the Parquet footer, so building the map costs no scanned rows and
the map is stable for the lifetime of the (immutable, published) file.

Chunk size is **derived, never configured**: it is the smallest of what the disk
allowance permits, what the measured build throughput can finish inside the
promotion time budget, and a hard row ceiling set below the row count that
OOM-killed the server during measurement. See
:func:`backend.db.budget.max_buildable_rows` and
:data:`backend.db.budget.PROMOTION_MAX_ROWS`.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta

from psycopg2 import sql

from backend.db.budget import CHUNK_ROWS
from backend.db.datasets import Dataset, DatasetKind
from backend.db.errors import refuse
from backend.db.pruning import RowGroup, load_row_groups

logger = logging.getLogger(__name__)

_PLAN_CACHE: dict[tuple[str, int], tuple[Chunk, ...]] = {}
_PLAN_LOCK = threading.Lock()


@dataclass(frozen=True)
class Chunk:
    """One addressable slice of a Parquet file."""

    chunk_id: str
    """Stable identifier, ``<file stem>:<first row group>-<last row group>``.

    Derived from the file and its row-group span rather than an ordinal, so the
    id means the same thing across runs and processes -- which is what lets a
    sweep resume from a cursor.
    """

    url: str
    first_row_group: int
    last_row_group: int
    num_rows: int
    compressed_bytes: int
    ts_lo: datetime
    ts_hi: datetime
    """Exclusive upper bound, nudged past the last row group's max timestamp so
    adjacent chunks tile without overlapping or dropping a boundary row."""

    @property
    def row_groups(self) -> int:
        return self.last_row_group - self.first_row_group + 1

    @property
    def seconds(self) -> float:
        return (self.ts_hi - self.ts_lo) / timedelta(seconds=1)

    @property
    def file_name(self) -> str:
        return self.url.rsplit("/", 1)[-1]

    def as_dict(self) -> dict:
        """Agent- and UI-facing summary. No URLs: those are server-side detail."""
        return {
            "chunk_id": self.chunk_id,
            "file": self.file_name,
            "rows": self.num_rows,
            "row_groups": self.row_groups,
            "start": self.ts_lo.isoformat(),
            "end": self.ts_hi.isoformat(),
            "seconds": round(self.seconds, 2),
            "compressed_bytes": self.compressed_bytes,
        }


def _bound_text(moment: datetime) -> str:
    """A timestamp literal SereneDB will accept for the corpus' column type."""
    return moment.isoformat(sep=" ")


def chunk_predicate(dataset: Dataset, chunk: Chunk) -> tuple[sql.Composable, list]:
    """``ts >= lo AND ts < hi`` for one chunk.

    Identical in shape to a normal time filter, which is the point: a promoted
    chunk is queried by the same builders as anything else, and the zonemaps do
    the narrowing.
    """
    cast = sql.SQL(dataset.ts_cast)
    clause = sql.SQL("{ts} >= {lo}::{cast} AND {ts} < {hi}::{cast}").format(
        ts=dataset.sql_for("ts"),
        lo=sql.Literal(_bound_text(chunk.ts_lo)),
        hi=sql.Literal(_bound_text(chunk.ts_hi)),
        cast=cast,
    )
    return clause, []


def _group_row_groups(
    url: str, groups: list[RowGroup], target_rows: int
) -> list[Chunk]:
    """Pack contiguous row groups into chunks of at most ``target_rows``.

    A single row group larger than the target still becomes its own chunk: the
    row group is the indivisible unit of pruning, so there is nothing smaller to
    address. On this corpus a row group is ~614k rows while the promotion
    ceiling is lower, so **every chunk is exactly one row group** and
    ``num_rows`` is rows *read*. The rows actually indexed are far fewer,
    because promotion applies a severity filter at build time.

    Chunk bounds tile on **successor boundaries**: each chunk ends where the
    next begins. That is required for correctness, not tidiness -- adjacent row
    groups genuinely overlap in time on this corpus (one group's max timestamp
    can exceed the next group's min, because rows sharing a timestamp get split
    across groups), so deriving an end bound from a group's own max would both
    overlap its successor and risk dropping boundary rows. Tiling on the
    successor's start makes every row belong to exactly one chunk.

    Timestamps are also truncated to microseconds here, because the column is
    nanosecond precision and Python's ``datetime`` is not. Successor tiling
    makes that harmless: the boundary value is shared by both sides, so no row
    can fall between two chunks regardless of the lost digits.
    """
    stem = url.rsplit("/", 1)[-1].removesuffix(".parquet")
    batches: list[list[RowGroup]] = []
    batch: list[RowGroup] = []

    for group in groups:
        if batch and sum(g.num_rows for g in batch) + group.num_rows > target_rows:
            batches.append(batch)
            batch = []
        batch.append(group)
    if batch:
        batches.append(batch)

    chunks: list[Chunk] = []
    for index, current in enumerate(batches):
        first, last = current[0], current[-1]
        if index + 1 < len(batches):
            upper = batches[index + 1][0].ts_min
        else:
            # Last chunk: nothing follows, so extend just past the final row.
            upper = last.ts_max + timedelta(microseconds=1)
        chunks.append(
            Chunk(
                chunk_id=f"{stem}:{first.row_group_id}-{last.row_group_id}",
                url=url,
                first_row_group=first.row_group_id,
                last_row_group=last.row_group_id,
                num_rows=sum(g.num_rows for g in current),
                compressed_bytes=sum(g.compressed_bytes for g in current),
                ts_lo=first.ts_min,
                ts_hi=upper,
            )
        )
    return chunks


def plan_chunks(
    dataset: Dataset,
    *,
    target_rows: int = CHUNK_ROWS,
    fetch=None,
) -> tuple[Chunk, ...]:
    """The chunk map for every file in ``dataset``. Reads footers only.

    Cached: the files are immutable, so the map is computed once per process.
    """
    if dataset.kind is not DatasetKind.REMOTE_PARQUET:
        raise refuse(
            f"'{dataset.id}' is not a remote Parquet corpus, so it has no chunk "
            "map.",
            "Sweep the remote corpus instead, or query this one directly -- it "
            "is small enough not to need chunking.",
            code="not_chunkable",
        )

    key = (dataset.id, target_rows)
    with _PLAN_LOCK:
        cached = _PLAN_CACHE.get(key)
    if cached is not None:
        return cached

    ts_column = dataset.field("ts").column or "Timestamp"
    chunks: list[Chunk] = []
    for url in dataset.sources:
        groups = load_row_groups(url, ts_column, fetch=fetch)
        chunks.extend(_group_row_groups(url, groups, target_rows))

    result = tuple(chunks)
    with _PLAN_LOCK:
        _PLAN_CACHE[key] = result

    logger.info(
        "Chunk map for %s: %d chunks over %d file(s), target %d rows/chunk",
        dataset.id,
        len(result),
        len(dataset.sources),
        target_rows,
    )
    return result


def clear_plan_cache() -> None:
    """Drop cached chunk maps. For tests and for a re-plan after resizing."""
    with _PLAN_LOCK:
        _PLAN_CACHE.clear()


def find_chunk(dataset: Dataset, chunk_id: str, *, fetch=None) -> Chunk:
    """Resolve a chunk id, or refuse with the valid range.

    Chunk ids reach here from model output, so an unknown one must produce a
    refusal rather than an exception.
    """
    for chunk in plan_chunks(dataset, fetch=fetch):
        if chunk.chunk_id == chunk_id:
            return chunk
    raise refuse(
        f"No chunk '{chunk_id}' in '{dataset.id}'.",
        "Use a chunk_id from the sweep's ranked chunk list.",
        code="unknown_chunk",
    )


def chunks_in_window(
    dataset: Dataset, start: datetime, end: datetime, *, fetch=None
) -> tuple[Chunk, ...]:
    """Chunks overlapping ``[start, end)``, so a sweep can be scoped."""
    return tuple(
        c for c in plan_chunks(dataset, fetch=fetch) if c.ts_hi > start and c.ts_lo < end
    )


def map_summary(dataset: Dataset, *, fetch=None) -> dict:
    """Totals for the chunk map, for the sweep's coverage statement."""
    chunks = plan_chunks(dataset, fetch=fetch)
    total_rows = sum(c.num_rows for c in chunks)
    return {
        "dataset": dataset.id,
        "files": len(dataset.sources),
        "chunks": len(chunks),
        "total_rows": total_rows,
        "rows_per_chunk": CHUNK_ROWS,
        "total_bytes": sum(c.compressed_bytes for c in chunks),
        "span_seconds": round(sum(c.seconds for c in chunks), 2),
    }
