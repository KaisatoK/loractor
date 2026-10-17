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

import logging
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
                estimated_bytes=plan.estimated_bytes,
            )

        report: dict = {
            "plan": plan.as_dict(),
            "budget": verdict.reason,
            "dry_run": dry_run,
            "copies_data": False,
        }
        if dry_run:
            return report

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

        report["rows"] = rows
        report["bytes"] = built - baseline
        report["bytes_per_row"] = round((built - baseline) / max(rows, 1), 1)
        report["within_allowance"] = (
            built - baseline
        ) <= BUDGET.otel_slice_allowance_bytes

    logger.info("External index built: %s", report)
    return report


def _drop(cur) -> None:
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
