"""Metadata pruning: decide what a query will read before running it.

The corpus is timestamp-sorted and every Parquet row group carries
``stats_min_value``/``stats_max_value`` for ``Timestamp``. Reading the footers is
free, so a time window can be costed -- which files, which row groups, how many
rows, how many bytes -- without scanning a single row.

That buys two things:

* a **pre-flight estimate**, so an over-large query is refused in milliseconds
  rather than timing out after thirty seconds;
* the **row and byte figures** in :class:`~backend.db.stats.QueryStats`, which is
  how the answer can state what it actually read.

The plan assumed row groups would be large time spans. They are not: a row group
is ~614k rows covering ~1.1 seconds, so pruning is fine-grained and a
few-second window really does touch only a handful of groups.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime

from backend.db.datasets import Dataset, DatasetKind
from backend.db.errors import refuse
from backend.db.stats import QueryStats
from backend.db.windows import TimeWindow

logger = logging.getLogger(__name__)

# Footer metadata per URL. Stable for the lifetime of the process: these are
# immutable published files, so there is nothing to invalidate.
_CACHE: dict[str, list[RowGroup]] = {}
_CACHE_LOCK = threading.Lock()

# Refuse a remote query whose pruned estimate exceeds this. At the measured
# ~6.6M rows/s for a narrow projection, 200M rows is ~30 s of pure scan -- past
# that the agent should narrow instead of waiting.
MAX_EST_ROWS_SCANNED = 200_000_000


@dataclass(frozen=True)
class RowGroup:
    """One row group's footer facts."""

    url: str
    row_group_id: int
    num_rows: int
    compressed_bytes: int
    ts_min: datetime
    ts_max: datetime

    def overlaps(self, window: TimeWindow, parse) -> bool:
        """Whether this group can contain rows inside ``window``.

        Half-open on the upper bound to match ``ts >= start AND ts < end``: a
        group starting exactly at ``end`` holds nothing the query wants.
        """
        start, end = parse(window.start), parse(window.end)
        return self.ts_max >= start and self.ts_min < end


@dataclass(frozen=True)
class PruningPlan:
    """What a window will read, decided from metadata alone."""

    files_considered: int
    files_read: int
    row_groups_considered: int
    row_groups_read: int
    est_rows_scanned: int
    est_bytes_read: int

    @property
    def files_pruned(self) -> int:
        return max(self.files_considered - self.files_read, 0)

    def as_stats(self, dataset: Dataset, window: TimeWindow) -> QueryStats:
        return QueryStats(
            dataset=dataset.id,
            approximate=not dataset.exact_counts,
            files_considered=self.files_considered,
            files_pruned=self.files_pruned,
            row_groups_considered=self.row_groups_considered,
            row_groups_read=self.row_groups_read,
            est_rows_scanned=self.est_rows_scanned,
            est_bytes_read=self.est_bytes_read,
            window_seconds=window.seconds,
        )


def _to_datetime(value: object) -> datetime:
    """Parse a footer stat or a window bound into a comparable datetime."""
    if isinstance(value, datetime):
        return value
    text = str(value).strip().replace(" ", "T").replace("Z", "+00:00")
    # Footer stats arrive as '2025-09-23 00:00:01.116880211+00'; fromisoformat
    # wants a two-digit offset and at most 6 fractional digits.
    if len(text) > 5 and (text[-3] == "+" or text[-3] == "-"):
        text = f"{text}:00"
    if "." in text:
        head, _, tail = text.partition(".")
        digits = ""
        rest = ""
        for index, char in enumerate(tail):
            if char.isdigit():
                digits += char
            else:
                rest = tail[index:]
                break
        text = f"{head}.{digits[:6]}{rest}"
    return datetime.fromisoformat(text)


def _naive_or_aware(value: datetime, like: datetime) -> datetime:
    """Align ``value``'s awareness with ``like`` so the two can be compared."""
    if (value.tzinfo is None) == (like.tzinfo is None):
        return value
    if like.tzinfo is None:
        return value.replace(tzinfo=None)
    from datetime import timezone

    return value.replace(tzinfo=timezone.utc)


def load_row_groups(url: str, ts_column: str, *, fetch=None) -> list[RowGroup]:
    """Footer row-group stats for ``url``, cached per process.

    ``fetch`` is injected by tests; in production it reads ``parquet_metadata``.
    """
    with _CACHE_LOCK:
        cached = _CACHE.get(url)
    if cached is not None:
        return cached

    rows = (fetch or _fetch_row_groups)(url, ts_column)
    groups = [
        RowGroup(
            url=url,
            row_group_id=int(r["row_group_id"]),
            num_rows=int(r["row_group_num_rows"]),
            compressed_bytes=int(r["row_group_compressed_bytes"] or 0),
            ts_min=_to_datetime(r["stats_min_value"]),
            ts_max=_to_datetime(r["stats_max_value"]),
        )
        for r in rows
    ]
    with _CACHE_LOCK:
        _CACHE[url] = groups
    return groups


def _fetch_row_groups(url: str, ts_column: str) -> list[dict]:
    """Read ``parquet_metadata`` for one file. Footer only, no rows scanned."""
    from backend.db.connection import db_cursor

    with db_cursor() as cur:
        cur.execute(
            "SELECT row_group_id, row_group_num_rows, "
            "       row_group_compressed_bytes, stats_min_value, stats_max_value "
            "FROM parquet_metadata(%s) WHERE path_in_schema = %s "
            "ORDER BY row_group_id",
            (url, ts_column),
        )
        return [dict(r) for r in cur.fetchall()]


def clear_cache() -> None:
    """Drop cached footers. For tests, and for a re-probe after config changes."""
    with _CACHE_LOCK:
        _CACHE.clear()


def seed_cache(url: str, groups: list[RowGroup]) -> None:
    """Install row-group metadata for ``url`` without touching the database.

    The seam that keeps pruning and cost estimation testable offline: footers are
    immutable published facts, so a fixture is as good as a fetch.
    """
    with _CACHE_LOCK:
        _CACHE[url] = groups


def synthetic_row_groups(
    url: str,
    count: int,
    *,
    rows_per_group: int = 614_400,
    seconds_per_group: float = 1.1,
    bytes_per_group: int = 25_240_000,
    start: str = "2025-09-23 00:00:00+00",
) -> list[RowGroup]:
    """Row groups shaped like the real corpus, for fixtures.

    Defaults are the measured values for ``part_000``: ~614,400 rows spanning
    ~1.1 s in ~25 MB, contiguous and timestamp-sorted.
    """
    from datetime import timedelta

    base = _to_datetime(start)
    groups: list[RowGroup] = []
    for index in range(count):
        lo = base + timedelta(seconds=seconds_per_group * index)
        hi = base + timedelta(seconds=seconds_per_group * (index + 1))
        groups.append(
            RowGroup(
                url=url,
                row_group_id=index,
                num_rows=rows_per_group,
                compressed_bytes=bytes_per_group,
                ts_min=lo,
                ts_max=hi,
            )
        )
    return groups


def plan(dataset: Dataset, window: TimeWindow, *, fetch=None) -> PruningPlan:
    """Cost ``window`` against ``dataset`` from metadata alone.

    For a non-remote dataset there are no footers to read, so the plan is empty
    and the cost meter simply reports timing.
    """
    if dataset.kind is not DatasetKind.REMOTE_PARQUET:
        return PruningPlan(0, 0, 0, 0, 0, 0)

    ts_column = dataset.field("ts").column or "Timestamp"
    considered = 0
    files_read = 0
    groups_read = 0
    rows = 0
    data_bytes = 0

    for url in dataset.sources:
        groups = load_row_groups(url, ts_column, fetch=fetch)
        considered += len(groups)
        if not groups:
            continue

        reference = groups[0].ts_min
        start = _naive_or_aware(_to_datetime(window.start), reference)
        end = _naive_or_aware(_to_datetime(window.end), reference)

        hits = [g for g in groups if g.ts_max >= start and g.ts_min < end]
        if hits:
            files_read += 1
            groups_read += len(hits)
            rows += sum(g.num_rows for g in hits)
            data_bytes += sum(g.compressed_bytes for g in hits)

    return PruningPlan(
        files_considered=len(dataset.sources),
        files_read=files_read,
        row_groups_considered=considered,
        row_groups_read=groups_read,
        est_rows_scanned=rows,
        est_bytes_read=data_bytes,
    )


def plan_and_guard(dataset: Dataset, window: TimeWindow, *, fetch=None) -> QueryStats:
    """Cost the window and refuse it if the estimate is too large.

    This is the pre-flight check that turns a thirty-second timeout into a
    millisecond refusal with a concrete suggestion.
    """
    pruning = plan(dataset, window, fetch=fetch)
    stats = pruning.as_stats(dataset, window)

    if pruning.est_rows_scanned > MAX_EST_ROWS_SCANNED:
        suggested = max(
            1,
            int(window.seconds * MAX_EST_ROWS_SCANNED / pruning.est_rows_scanned),
        )
        raise refuse(
            f"That window would scan about {pruning.est_rows_scanned:,} rows "
            f"({pruning.row_groups_read} row groups), over the "
            f"{MAX_EST_ROWS_SCANNED:,}-row limit for a single query.",
            f"Narrow the window to roughly {suggested} second(s), or add a "
            "service or severity filter and try again.",
            code="scan_too_large",
            est_rows_scanned=pruning.est_rows_scanned,
            max_rows_scanned=MAX_EST_ROWS_SCANNED,
            suggested_window_seconds=suggested,
        )

    logger.debug(
        "pruned %s: %d/%d row groups, ~%d rows, ~%d bytes",
        dataset.id,
        pruning.row_groups_read,
        pruning.row_groups_considered,
        pruning.est_rows_scanned,
        pruning.est_bytes_read,
    )
    return stats
