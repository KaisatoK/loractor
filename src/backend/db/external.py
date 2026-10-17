"""Index the remote corpus in place, without copying it.

SereneDB can build an inverted index directly over external Parquet: point a
view at ``read_parquet`` and index the view. The rows stay in object storage and
only the postings are stored locally, so a corpus far larger than the disk
becomes searchable without an ingest step.

Measured against the materialized alternative, over the same two row groups:

===================  ==================  ====================
\\                    external index      materialized slice
===================  ==================  ====================
build (cold)         **464.0 s**         1,323.6 s
disk                 **60.1 B/row**      154.9 B/row
rows copied          **none**            150,557
search latency       1.6 s               0.02 s
===================  ==================  ====================

So indexing in place is ~2.9x faster to build and ~2.6x cheaper on disk. The
reason build is cheaper than the materialized path -- which has to *read* the
same text -- is that only the indexed columns are read, with projection and
predicate pruning pushed into the reader.

The one cost is search latency. A match yields a row identity, and the row is
re-read from object storage to materialize its columns, so a query is ~1.6 s
rather than ~20 ms. For log investigation that is the right trade: a second and
a half to search a corpus that was never downloaded.

Two details worth knowing, both from the documentation and confirmed here:

* **Row identity is automatic.** A Parquet-backed view keys on
  ``file_row_number``, so there is no need to synthesize an id -- which was the
  only reason the slice was materialized in the first place.
* **The index is a snapshot.** It does not follow changes to the files;
  ``REINDEX INDEX`` brings it up to date in one pass.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
import threading
import time
from dataclasses import dataclass

from psycopg2 import sql

from backend.db.budget import BUDGET, PROBLEM_SEVERITIES, fits
from backend.db.datasets import Dataset, resolve
from backend.db.errors import refuse
from backend.db.corpus import arrival_rate, ensure_dictionaries, row_groups_touched

logger = logging.getLogger(__name__)

# Disk per indexed row for an external index. Measured 60.1 B/row across two
# row groups, against 154.9 for the materialized path.
EXTERNAL_BYTES_PER_ROW = 62

# Seconds for one external index build, cold. **Fixed, not per row group.**
#
# Measured, and the flatness is the whole point:
#
#    row groups   build       rows indexed   B/row
#    1            468.3 s        72,678       60.0
#    2            464.0 s       180,269       60.1
#    4            467.8 s       331,425       45.8
#    15           465.3 s     1,401,135       44.8
#
# Indexing fifteen row groups costs the same as indexing one, and the postings
# compress *better* with more data. The cost is dominated by a fixed per-build
# scan of the file, which parallelises across row groups; the marginal cost of a
# wider window is near zero up to at least 15 groups.
#
# Against materializing, which pays ~660 s *per row group*: 1.4M rows indexed in
# 465 s is 0.33 ms/row, where the materialized slice managed 8.8 ms/row -- about
# 27x more efficient, for a third of the disk and no copy.
EXTERNAL_BUILD_SECONDS = 470.0

# Row groups beyond which build time is no longer known to be flat. Measured
# flat to 15; beyond that the model is an extrapolation and says so.
MEASURED_FLAT_ROW_GROUPS = 15

# Longest an index build may be planned to take. Same reasoning as the slice:
# a build is not resumable and holds its working set throughout.
MAX_BUILD_SECONDS = 1_800

VIEW_NAME = "otel_ext"
INDEX_NAME = "otel_ext_idx"

# One-row build record: the window and severities the index covers. The view's
# WHERE clause is the only other place that knows, so without this the catalog
# (``backend.db.catalog_query``) could not say what the index can find.
META_TABLE = "otel_ext_meta"


@dataclass(frozen=True)
class ExternalIndexPlan:
    """What an external index build will cover, and what it will cost."""

    start: str
    end: str
    severities: tuple[str, ...]
    window_seconds: float
    row_groups: int
    estimated_rows: int
    estimated_bytes: int
    estimated_seconds: float

    def as_dict(self) -> dict:
        return {
            "start": self.start,
            "end": self.end,
            "severities": list(self.severities),
            "window_seconds": round(self.window_seconds, 2),
            "row_groups": self.row_groups,
            "estimated_rows": self.estimated_rows,
            "estimated_bytes": self.estimated_bytes,
            "estimated_seconds": round(self.estimated_seconds, 1),
            "copies_data": False,
        }


def plan_external_index(
    start: str,
    end: str,
    *,
    severities: tuple[str, ...] = PROBLEM_SEVERITIES,
    dataset: Dataset | str = "otel_s3",
    max_seconds: int = MAX_BUILD_SECONDS,
) -> ExternalIndexPlan:
    """Price an external index over a window before building it.

    Refuses a window it could not finish, rather than starting a build that
    would be abandoned. The binding cost is row groups touched, exactly as for
    the materialized path -- text has to be read either way.
    """
    from datetime import datetime

    ds = resolve(dataset) if isinstance(dataset, str) else dataset
    begin = datetime.fromisoformat(start.replace(" ", "T"))
    finish = datetime.fromisoformat(end.replace(" ", "T"))
    if finish <= begin:
        raise refuse(
            "The window ends before it starts.",
            "Give an end time after the start time.",
            code="inverted_window",
        )

    seconds = (finish - begin).total_seconds()
    groups = row_groups_touched(seconds)
    # Flat within the measured range; beyond it, assume the fixed cost scales
    # with each further block of that size rather than claiming it stays flat.
    blocks = max(1, -(-groups // MEASURED_FLAT_ROW_GROUPS))
    estimated_seconds = blocks * EXTERNAL_BUILD_SECONDS

    if estimated_seconds > max_seconds:
        affordable = MEASURED_FLAT_ROW_GROUPS * max(
            1, int(max_seconds / EXTERNAL_BUILD_SECONDS)
        )
        raise refuse(
            f"Indexing {seconds:.0f}s of corpus spans {groups} row groups and "
            f"would take about {estimated_seconds / 60:.0f} minutes.",
            f"Index at most {affordable} row group(s) at a time -- roughly "
            f"{affordable * 1.1:.1f}s of corpus -- or raise the build budget.",
            code="build_too_long",
            row_groups=groups,
            estimated_seconds=estimated_seconds,
        )

    rows = int(seconds * arrival_rate(severities))
    return ExternalIndexPlan(
        start=start,
        end=end,
        severities=severities,
        window_seconds=seconds,
        row_groups=groups,
        estimated_rows=rows,
        estimated_bytes=rows * EXTERNAL_BYTES_PER_ROW,
        estimated_seconds=estimated_seconds,
    )


_SIZE_UNITS = {
    "b": 1, "kb": 1000, "mb": 1000**2, "gb": 1000**3, "tb": 1000**4,
    "kib": 1024, "mib": 1024**2, "gib": 1024**3, "tib": 1024**4,
}


def parse_byte_size(text: str) -> int | None:
    """Bytes from a size as SereneDB reports it (``5.7 GiB``), else ``None``."""
    match = re.fullmatch(
        r"\s*([0-9]*\.?[0-9]+)\s*([KMGT]?i?B|B)?\s*", str(text), re.IGNORECASE
    )
    if not match:
        return None
    unit = (match.group(2) or "B").lower()
    if unit not in _SIZE_UNITS:
        return None
    return int(float(match.group(1)) * _SIZE_UNITS[unit])


def hold_memory_ceiling() -> dict:
    """Hold the server to the budgeted memory ceiling before a build.

    ``memory_limit`` is global and does **not** survive a container restart, and
    a restarted SereneDB came back at 5.7 GiB -- the very value the budget
    records as the cause of three OOM kills. Building the in-place index is the
    operation that breaches it: two of three builds in one session were killed
    mid-``CREATE INDEX``, each time taking the instance down and changing its
    host port.

    So the ceiling is applied here rather than left to an operator who has no
    way of knowing the container bounced. A ceiling the planner respects turns
    an out-of-memory *crash* into a failed query, which the caller can report.
    A limit already at or below the budget is left alone, so a deliberately
    tighter setting is never raised.
    """
    from backend.db.connection import current_memory_limit, set_global_memory_limit

    budget_bytes = BUDGET.memory_limit_bytes
    try:
        before = current_memory_limit()
        current = parse_byte_size(before)
        if current is not None and current <= budget_bytes:
            return {"changed": False, "memory_limit": before}
        applied = set_global_memory_limit()
    except Exception as error:  # noqa: BLE001 -- never block a build on this
        logger.warning("Could not read or set the SereneDB memory ceiling: %s", error)
        return {"changed": False, "memory_limit": None, "error": str(error)}
    logger.warning(
        "Lowered the SereneDB memory ceiling from %s to %s before the index "
        "build (budget %.0f MiB); it reverts when the container restarts.",
        before, applied, budget_bytes / (1024 * 1024),
    )
    return {"changed": True, "memory_limit": applied, "previous": before}


def _cursor():
    from backend.db.probe import probe_cursor

    return probe_cursor()


def _db_bytes(cur) -> int:
    cur.execute("SELECT pg_database_size(current_database()) AS b")
    return int(cur.fetchone()["b"])


def build_external_index(
    start: str,
    end: str,
    *,
    severities: tuple[str, ...] = PROBLEM_SEVERITIES,
    dataset: Dataset | str = "otel_s3",
    plan: ExternalIndexPlan | None = None,
    dry_run: bool = False,
    progress=None,
) -> dict:
    """Build a searchable index over the remote corpus, copying nothing.

    The view carries the time and severity predicate, so the index covers
    exactly the window asked for and the reader prunes everything else at build
    time. Only the indexed columns are read.
    """
    ds = resolve(dataset) if isinstance(dataset, str) else dataset
    plan = plan or plan_external_index(
        start, end, severities=severities, dataset=ds
    )

    with _cursor() as cur:
        # The index being replaced is dropped before the new one is created, so
        # its bytes are credited back here. Charging the cap for both at once
        # refused builds for space the build itself was about to free.
        reclaimable = _recorded_index_bytes(cur)
        before_drop = _db_bytes(cur)
        verdict = fits(
            plan.estimated_bytes,
            dataset_id="otel_slice",
            current_db_bytes=before_drop - reclaimable,
        )
        if not verdict.ok:
            raise refuse(
                verdict.reason,
                verdict.suggestion,
                code="over_budget",
                estimated_bytes=plan.estimated_bytes,
            )

        report: dict = {
            "plan": plan.as_dict(),
            "budget": verdict.reason,
            "dry_run": dry_run,
            "copies_data": False,
            "reclaimable_bytes": reclaimable,
        }
        if dry_run:
            return report

        ceiling = hold_memory_ceiling()
        report["memory_ceiling"] = ceiling.get("memory_limit")
        if ceiling.get("changed") and progress is not None:
            progress(
                f"Lowered the database memory ceiling to "
                f"{ceiling['memory_limit']} for the build."
            )

        if progress is not None:
            progress(
                f"Indexing {plan.row_groups} row group(s) in place; "
                f"estimated {plan.estimated_seconds / 60:.0f} minutes."
            )
        logger.info("Building external index over %s..%s", plan.start, plan.end)

        _drop(cur)
        # Baseline *after* dropping any previous index, so replacing a larger
        # one does not report a negative size.
        cur.execute("VACUUM")
        baseline = _db_bytes(cur)
        # What dropping the previous index actually returned, measured rather
        # than assumed: only one index exists at a time, so this is the peak
        # the build would otherwise have added on top of itself.
        report["reclaimed_bytes"] = max(0, before_drop - baseline)
        ensure_dictionaries(cur)

        severity_list = sql.SQL(", ").join(
            sql.Literal(s) for s in plan.severities
        )
        # The view is the whole mechanism: predicates here are pushed into the
        # reader, so the index covers the window and nothing else is read.
        cur.execute(
            sql.SQL("""
                CREATE VIEW {view} AS
                SELECT "Timestamp", "SeverityText", "ServiceName", "Body",
                       "TraceId"
                FROM {source}
                WHERE "Timestamp" >= {lo}::timestamptz
                  AND "Timestamp" <  {hi}::timestamptz
                  AND "SeverityText" IN ({severities})
            """).format(
                view=sql.Identifier(VIEW_NAME),
                source=ds.scan_relation(),
                lo=sql.Literal(plan.start),
                hi=sql.Literal(plan.end),
                severities=severity_list,
            )
        )

        started = time.monotonic()
        cur.execute(
            sql.SQL("""
                CREATE INDEX {idx} ON {view} USING inverted (
                    "Body" english, "ServiceName", "SeverityText"
                )
            """).format(
                idx=sql.Identifier(INDEX_NAME), view=sql.Identifier(VIEW_NAME)
            )
        )
        report["build_seconds"] = round(time.monotonic() - started, 2)

        cur.execute(
            sql.SQL("SELECT count(*) AS n FROM {idx}").format(
                idx=sql.Identifier(INDEX_NAME)
            )
        )
        rows = int(cur.fetchone()["n"])
        built = _db_bytes(cur)
        _record_build(cur, plan, rows, index_bytes=built - baseline)

        report["rows"] = rows
        report["bytes"] = built - baseline
        report["bytes_per_row"] = round((built - baseline) / max(rows, 1), 1)
        report["within_allowance"] = (
            built - baseline
        ) <= BUDGET.otel_slice_allowance_bytes

    logger.info("External index built: %s", report)
    return report


def record_build_statements(
    plan: ExternalIndexPlan, rows: int, index_bytes: int = 0
) -> list[tuple[sql.Composable, tuple]]:
    """The statements that (re)write the one-row build record.

    Severities are stored comma-joined and the bounds as the plan's own text, so
    the record reads back exactly as the window was asked for.

    ``index_bytes`` is the disk the build measured. It is kept because SereneDB
    will not size an external index on request, and without it a replacing
    build cannot credit back the space it is about to free.
    """
    meta = sql.Identifier(META_TABLE)
    return [
        (sql.SQL("DROP TABLE IF EXISTS {t}").format(t=meta), ()),
        (
            sql.SQL(
                "CREATE TABLE {t} (window_start TEXT NOT NULL, "
                "window_end TEXT NOT NULL, severities TEXT NOT NULL, "
                "indexed_rows BIGINT NOT NULL, built_at TEXT NOT NULL, "
                "index_bytes BIGINT NOT NULL)"
            ).format(t=meta),
            (),
        ),
        (
            sql.SQL(
                "INSERT INTO {t} (window_start, window_end, severities, "
                "indexed_rows, built_at, index_bytes) "
                "VALUES (%s, %s, %s, %s, %s, %s)"
            ).format(t=meta),
            (
                plan.start,
                plan.end,
                ",".join(plan.severities),
                rows,
                time.strftime("%Y-%m-%d %H:%M:%S+00", time.gmtime()),
                max(0, int(index_bytes)),
            ),
        ),
    ]


def _record_build(cur, plan: ExternalIndexPlan, rows: int,
                  *, index_bytes: int = 0) -> None:
    for statement, params in record_build_statements(plan, rows, index_bytes):
        cur.execute(statement, params or None)


def _drop(cur) -> None:
    cur.execute(
        sql.SQL("DROP TABLE IF EXISTS {t}").format(t=sql.Identifier(META_TABLE))
    )
    cur.execute(
        sql.SQL("DROP INDEX IF EXISTS {idx}").format(
            idx=sql.Identifier(INDEX_NAME)
        )
    )
    cur.execute(
        sql.SQL("DROP VIEW IF EXISTS {view}").format(
            view=sql.Identifier(VIEW_NAME)
        )
    )


def drop_external_index() -> dict:
    """Remove the index and its view, reporting the disk reclaimed."""
    with _cursor() as cur:
        before = _db_bytes(cur)
        _drop(cur)
        cur.execute("VACUUM")
        after = _db_bytes(cur)
    return {"freed_bytes": before - after, "db_bytes": after}


def refresh_external_index() -> dict:
    """Bring the index up to date with the files behind it.

    An external index is a snapshot: it does not follow changes to the source.
    One pass re-reads only what appeared, changed or disappeared.
    """
    with _cursor() as cur:
        started = time.monotonic()
        cur.execute(
            sql.SQL("REINDEX INDEX {idx}").format(
                idx=sql.Identifier(INDEX_NAME)
            )
        )
        return {"reindex_seconds": round(time.monotonic() - started, 2)}


def external_index_status() -> dict:
    """Whether the in-place index exists, how big it is, and what it covers."""
    with _cursor() as cur:
        cur.execute(
            "SELECT count(*) AS n FROM information_schema.tables "
            "WHERE table_name = %s",
            (VIEW_NAME,),
        )
        if not int(cur.fetchone()["n"]):
            return {"exists": False, "index": INDEX_NAME}

        cur.execute(
            sql.SQL("SELECT count(*) AS n FROM {idx}").format(
                idx=sql.Identifier(INDEX_NAME)
            )
        )
        rows = int(cur.fetchone()["n"])

    return {
        "exists": True,
        "index": INDEX_NAME,
        "rows": rows,
        "copies_data": False,
        "note": (
            "Rows stay in object storage; only the postings are local. A match "
            "re-reads its row from the file, so search is slower than on a "
            "copied table but nothing was downloaded."
        ),
    }


# --- Keeping the index in step with what is being searched --------------------
# The index is built over a window (see ``build_external_index``), and the
# window it covers is recorded in ``META_TABLE``. A search is therefore only
# answerable from the index when its window falls inside that one, so the search
# path asks for coverage first and builds when it is missing.
#
# There is no effort gate and no cost confirmation here, deliberately: a search
# on ``otel_slice`` that cannot be answered is worth less than the minutes the
# build costs, and the previous gating meant the agent discovered the cost by
# failing. Builds are serialized so two concurrent searches cannot index at
# once.
_BUILD_LOCK = threading.Lock()


def _as_utc(value: str) -> dt.datetime:
    """Parse a window bound, reading a naive one as UTC.

    Every session is pinned to UTC (``connection.set_session_time_zone``), and
    the build record stores the bounds as the text they were asked for, so a
    naive bound and an aware one must still compare.
    """
    parsed = dt.datetime.fromisoformat(str(value).strip().replace(" ", "T"))
    return parsed.replace(tzinfo=dt.timezone.utc) if parsed.tzinfo is None else parsed


def _index_present(cur) -> bool:
    """Whether the inverted index itself exists, not merely its view.

    SereneDB does not expose an external index to ``pg_relation_size`` or
    ``information_schema``, so it is probed by name. The view outliving the
    index is a real state -- a build creates the view first and a drop can
    leave it behind -- and treating the view as proof of an index let a search
    run against a relation with nothing indexed. ``probe_cursor`` is
    autocommit, so the failed probe does not poison a transaction and needs no
    transaction-scoped savepoint of the kind SereneDB rejects.
    """
    try:
        cur.execute(
            sql.SQL("SELECT count(*) AS n FROM {idx}").format(
                idx=sql.Identifier(INDEX_NAME)
            )
        )
        cur.fetchone()
        return True
    except Exception:  # noqa: BLE001 -- absence is the answer, not an error
        return False


def _recorded_index_bytes(cur) -> int:
    """Disk the current index measured at build time, or 0 when unknown.

    Stored in the build record because SereneDB will not size an external
    index on request. It is what a replacing build may credit back: the old
    index is dropped before the new one is created, so charging the budget for
    both at once would refuse a build for space it is about to free.
    """
    try:
        cur.execute(
            sql.SQL("SELECT index_bytes FROM {t} LIMIT 1").format(
                t=sql.Identifier(META_TABLE)
            )
        )
        row = cur.fetchone()
    except Exception:  # noqa: BLE001 -- no record, or one written before this column
        return 0
    return int(row["index_bytes"] or 0) if row else 0


def covers_window(start: str | None, end: str | None,
                  window_start: str | None, window_end: str | None) -> bool:
    """Whether ``start..end`` falls inside the indexed ``window_start..end``.

    False whenever that cannot be established -- an unasked or unrecorded
    window, or a bound that will not parse -- so unknown coverage leads to a
    build rather than to a search of an index that does not hold the rows.
    """
    if not (start and end and window_start and window_end):
        return False
    try:
        return (
            _as_utc(start) >= _as_utc(window_start)
            and _as_utc(end) <= _as_utc(window_end)
        )
    except ValueError:
        logger.warning(
            "Could not compare %s..%s with the index record %s..%s",
            start, end, window_start, window_end,
        )
        return False


def index_coverage(start: str | None = None, end: str | None = None) -> dict:
    """What the current in-place index covers, and whether it covers a window.

    ``covers`` is ``False`` whenever that cannot be established -- no index, no
    build record, or an unparseable bound -- so an unknown coverage leads to a
    build rather than to a search that silently finds nothing.
    """
    coverage = {
        "index": INDEX_NAME,
        "exists": False,
        "covers": False,
        "window_start": None,
        "window_end": None,
        "severities": list(PROBLEM_SEVERITIES),
        "index_bytes": None,
    }
    with _cursor() as cur:
        cur.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_name IN (%s, %s)",
            (VIEW_NAME, META_TABLE),
        )
        present = {row["table_name"] for row in cur.fetchall()}
        if VIEW_NAME not in present or not _index_present(cur):
            return coverage
        coverage["exists"] = True
        if META_TABLE not in present:
            return coverage
        coverage["index_bytes"] = _recorded_index_bytes(cur) or None
        cur.execute(
            sql.SQL(
                "SELECT window_start, window_end, severities FROM {t} LIMIT 1"
            ).format(t=sql.Identifier(META_TABLE))
        )
        record = cur.fetchone()

    if not record:
        return coverage
    coverage["window_start"] = str(record["window_start"])
    coverage["window_end"] = str(record["window_end"])
    coverage["severities"] = [
        s for s in str(record["severities"] or "").split(",") if s
    ]
    coverage["covers"] = covers_window(
        start, end, coverage["window_start"], coverage["window_end"]
    )
    return coverage


def release_external_index() -> dict:
    """Return the index's disk, reporting what was reclaimed.

    Serialized with the builds so a release cannot land in the middle of one.
    The next search over an uncovered window rebuilds, which costs minutes, so
    this is for reclaiming space that is genuinely no longer wanted.
    """
    with _BUILD_LOCK:
        before = index_coverage()
        freed = drop_external_index()
        return {
            "released": before["exists"],
            "freed_bytes": freed["freed_bytes"],
            "db_bytes": freed["db_bytes"],
            "window_start": before["window_start"],
            "window_end": before["window_end"],
            "note": (
                "The next search over an uncovered window rebuilds the index, "
                "which takes minutes."
            ),
        }


def ensure_external_index(start: str, end: str, *,
                          severities: tuple[str, ...] = PROBLEM_SEVERITIES,
                          progress=None) -> dict:
    """Make the window searchable, building the index when it is not covered.

    Returns the coverage plus ``built``: ``False`` when the existing index
    already answered for this window, ``True`` when one was built for it. A
    build replaces the previous index, so coverage moves to the new window.
    """
    with _BUILD_LOCK:
        coverage = index_coverage(start, end)
        if coverage["covers"]:
            return {"built": False, **coverage}
        if progress is not None:
            progress(
                "The in-place OTel index does not cover "
                f"{start}..{end}; building it over that window now."
            )
        report = build_external_index(
            start, end, severities=severities, progress=progress
        )
        built = index_coverage(start, end)
        return {
            "built": True,
            "build_seconds": report.get("build_seconds"),
            "indexed_rows": report.get("rows"),
            "bytes": report.get("bytes"),
            # Space the replaced index returned, so one index's worth of disk
            # is held at a time rather than two.
            "reclaimed_bytes": report.get("reclaimed_bytes"),
            # The ceiling the build actually ran under; it reverts when the
            # container restarts, so it is reported per build.
            "memory_ceiling": report.get("memory_ceiling"),
            **built,
        }


def ensure_searchable(start: str | None, end: str | None, *, progress=None) -> dict:
    """Coverage for a search on ``otel_slice``, indexing the window if needed.

    A search without a window cannot say what to index, so it is answered from
    whatever index exists and refused -- with the fix -- when there is none.
    """
    if start is None or end is None:
        coverage = index_coverage()
        if not coverage["exists"]:
            raise refuse(
                "The in-place OTel index has not been built, and a search "
                "without a time window does not say what to index.",
                "Search again with start_time and end_time; that window is "
                "indexed automatically before the search runs.",
                code="index_window_required",
            )
        return {"built": False, **coverage}
    return ensure_external_index(start, end, progress=progress)
