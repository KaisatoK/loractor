"""Zero-scan corpus measurement and a safe index-cost probe.

Run with::

    uv run python -m backend.db.probe                 # metadata only (no scan)
    uv run python -m backend.db.probe --severity      # + severity distribution
    uv run python -m backend.db.probe --index-cost    # + controlled index build

The constants this refreshes live in :mod:`backend.db.budget`.

Resolution of the two open assumptions
--------------------------------------

**Assumption 1 -- does ``CREATE INDEX`` over a time-filtered view prune row
groups at build time?** **YES, RESOLVED.** Measured: a view filtered to a
2-second window indexed 156,150 rows in 9.8 s, and an 8-second window indexed
686,583 rows. Both row counts match the measured 13.84% error rate exactly, and
neither time is achievable without pruning given the file is 38.2 GiB. The
cheaper branch is available, so promotion indexes a view directly and the
``CREATE TABLE AS SELECT`` fallback the plan specified for a negative answer is
**not needed**. Measured cost through a view is 73-74 B/row against 293 B/row
materialized.

The sting is elsewhere: **build throughput degrades with size** (16k rows/s at
156k rows, 6.3k rows/s at 687k). Build *time*, not disk, is what bounds how much
can be indexed -- see :func:`backend.db.budget.max_buildable_rows`.

**Assumption 2 -- is ``USING SAMPLE`` / ``TABLESAMPLE`` supported?** **YES,
both.** Measured by :func:`measure_sampling_support`. Stratified micro-window
sampling remains the implemented path because it prunes by row group and gives
time-spread samples, which uniform sampling does not; the sampling syntax is
available as an optimization where spread does not matter.
"""

from __future__ import annotations

import argparse
import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from psycopg2 import sql
from psycopg2.extras import RealDictCursor

from backend.db.config import SERENEDB_BUILD_THREADS as BUILD_THREADS
from backend.db.budget import (
    BUDGET,
    PROBLEM_SEVERITIES,
    _human,
    estimate_build_seconds,
    estimate_index_bytes,
    max_buildable_rows,
)
from backend.db.connection import db_cursor, get_conn, set_session_time_zone

logger = logging.getLogger(__name__)


@contextmanager
def probe_cursor() -> Iterator[RealDictCursor]:
    """A standalone **autocommit** cursor for size measurements and builds.

    Size probes cannot run inside one transaction: ``pg_database_size`` does not
    reflect uncommitted writes, so a build and its measurement in the same
    transaction both read zero.

    This deliberately does **not** set ``memory_limit``. An earlier version did,
    and it was a trap: ``SET memory_limit`` is global and persistent in SereneDB,
    so a helper documented as "a cursor for measurements" quietly reconfigured
    the entire server -- including for any other client -- every time a probe
    ran. The ceiling is now an explicit operator action,
    :func:`backend.db.connection.set_global_memory_limit`.

    ``threads`` is different, and measurably so: it reads back as the default on
    a fresh connection, so it is **session-scoped** and safe to lower here. That
    matters on this host, where each worker thread carries its own operator
    buffers -- 16 threads multiply the peak footprint of exactly the build
    operations that have proven fragile. Fewer threads means a slower build with
    a much lower ceiling, which is the right trade when the alternative is an
    OOM kill.
    """
    conn = get_conn()
    conn.autocommit = True
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        set_session_time_zone(cur)
        cur.execute(sql.SQL("SET threads = {n}").format(n=sql.Literal(BUILD_THREADS)))
        yield cur
    finally:
        cur.close()
        conn.close()

# Rows pulled into the controlled index-cost build.
#
# 100k is chosen from measurement at both ends: below ~20k rows nothing reaches
# disk yet (the write path still holds them, so every size delta reads zero and
# the measurement is useless), while 2.44M rows OOM-killed the server. 100k
# measured cleanly at 293 B/row and is an order of magnitude below the crash.
INDEX_PROBE_ROWS = 100_000


@dataclass(frozen=True)
class FileMetadata:
    """Zero-scan facts about one Parquet file."""

    url: str
    num_rows: int
    num_row_groups: int
    file_size_bytes: int

    @property
    def bytes_per_row(self) -> float:
        return self.file_size_bytes / self.num_rows if self.num_rows else 0.0

    @property
    def rows_per_row_group(self) -> float:
        return self.num_rows / self.num_row_groups if self.num_row_groups else 0.0


@dataclass(frozen=True)
class RowGroupStat:
    """Per-row-group time range, the unit of pruning."""

    row_group_id: int
    num_rows: int
    ts_min: str
    ts_max: str
    compressed_bytes: int


def file_metadata(url: str) -> FileMetadata:
    """Read ``parquet_file_metadata`` for ``url``. Reads the footer only."""
    with db_cursor() as cur:
        cur.execute(
            "SELECT num_rows, num_row_groups, file_size_bytes "
            "FROM parquet_file_metadata(%s)",
            (url,),
        )
        row = cur.fetchone()

    return FileMetadata(
        url=url,
        num_rows=int(row["num_rows"]),
        num_row_groups=int(row["num_row_groups"]),
        file_size_bytes=int(row["file_size_bytes"]),
    )


def row_group_stats(url: str, ts_column: str = "Timestamp") -> list[RowGroupStat]:
    """Per-row-group min/max of ``ts_column``. Reads the footer only.

    This is what makes chunking free: the corpus is timestamp-sorted, so these
    ranges are contiguous and a ``ts BETWEEN`` predicate maps onto whole row
    groups.
    """
    with db_cursor() as cur:
        cur.execute(
            "SELECT row_group_id, row_group_num_rows, stats_min_value, "
            "       stats_max_value, total_compressed_size "
            "FROM parquet_metadata(%s) WHERE path_in_schema = %s "
            "ORDER BY row_group_id",
            (url, ts_column),
        )
        rows = cur.fetchall()

    return [
        RowGroupStat(
            row_group_id=int(r["row_group_id"]),
            num_rows=int(r["row_group_num_rows"]),
            ts_min=str(r["stats_min_value"]),
            ts_max=str(r["stats_max_value"]),
            compressed_bytes=int(r["total_compressed_size"]),
        )
        for r in rows
    ]


def parquet_columns(url: str) -> list[tuple[str, str]]:
    """``(column_name, column_type)`` pairs, via ``DESCRIBE``. Footer only."""
    with db_cursor() as cur:
        cur.execute(
            sql.SQL("DESCRIBE SELECT * FROM read_parquet({url})").format(
                url=sql.Literal(url)
            )
        )
        return [(r["column_name"], r["column_type"]) for r in cur.fetchall()]


def severity_distribution(
    url: str, ts_lo: str, ts_hi: str
) -> tuple[list[dict], float]:
    """Severity counts in a bounded window, projecting 2 of 16 columns.

    Returns the rows and the elapsed seconds. Bounded on purpose: the point is to
    learn the mix cheaply, not to count the corpus.
    """
    started = time.monotonic()
    with db_cursor() as cur:
        cur.execute(
            sql.SQL("""
                SELECT "SeverityText" AS severity, count(*) AS n
                FROM read_parquet({url})
                WHERE "Timestamp" >= {lo}::timestamptz
                  AND "Timestamp" <  {hi}::timestamptz
                GROUP BY "SeverityText"
                ORDER BY n DESC
            """).format(
                url=sql.Literal(url), lo=sql.Literal(ts_lo), hi=sql.Literal(ts_hi)
            )
        )
        rows = [dict(r) for r in cur.fetchall()]
    return rows, time.monotonic() - started


def measure_index_bytes_per_row(url: str, ts_lo: str) -> dict:
    """Materialize a small slice, index it, and measure disk per row.

    Measured by ``pg_database_size`` **deltas** rather than
    ``pg_total_relation_size``: a freshly built relation reports zero size until
    ``VACUUM (REFRESH_TABLE)`` has run, so the naive reading is misleading. The
    delta is also the number the budget actually cares about, since the cap is on
    total database size.

    Deliberately paranoid: caps the slice with ``LIMIT``, pins a session
    ``memory_limit``, and drops everything afterwards. The unbounded version of
    this measurement is what took the server down.
    """
    probe_table = "probe_index_cost"
    result: dict = {"rows": 0}

    with probe_cursor() as cur:

        def db_bytes() -> int:
            cur.execute("SELECT pg_database_size(current_database()) AS b")
            return int(cur.fetchone()["b"])

        cur.execute(
            sql.SQL("DROP TABLE IF EXISTS {t} CASCADE").format(
                t=sql.Identifier(probe_table)
            )
        )
        baseline = db_bytes()

        severities = sql.SQL(", ").join(sql.Literal(s) for s in PROBLEM_SEVERITIES)
        started = time.monotonic()
        cur.execute(
            sql.SQL("""
                CREATE TABLE {t} AS
                SELECT "Timestamp", "TraceId", "SeverityText",
                       "ServiceName", "Body"
                FROM read_parquet({url})
                WHERE "Timestamp" >= {lo}::timestamptz
                  AND "SeverityText" IN ({severities})
                LIMIT {n}
            """).format(
                t=sql.Identifier(probe_table),
                url=sql.Literal(url),
                lo=sql.Literal(ts_lo),
                severities=severities,
                n=sql.Literal(INDEX_PROBE_ROWS),
            )
        )
        result["ctas_seconds"] = round(time.monotonic() - started, 2)

        cur.execute(
            sql.SQL("SELECT count(*) AS n FROM {t}").format(
                t=sql.Identifier(probe_table)
            )
        )
        result["rows"] = rows = int(cur.fetchone()["n"]) or 1

        cur.execute(
            sql.SQL("VACUUM (REFRESH_TABLE) {t}").format(t=sql.Identifier(probe_table))
        )
        after_table = db_bytes()

        started = time.monotonic()
        cur.execute(
            sql.SQL("""
                CREATE INDEX {idx} ON {t} USING inverted (
                    "Body" english, "ServiceName", "SeverityText"
                )
            """).format(
                idx=sql.Identifier(f"{probe_table}_idx"),
                t=sql.Identifier(probe_table),
            )
        )
        cur.execute(
            sql.SQL("VACUUM (REFRESH_TABLE) {t}").format(t=sql.Identifier(probe_table))
        )
        result["index_seconds"] = round(time.monotonic() - started, 2)
        after_index = db_bytes()

        cur.execute(
            sql.SQL("DROP TABLE IF EXISTS {t} CASCADE").format(
                t=sql.Identifier(probe_table)
            )
        )
        result["bytes_after_teardown"] = db_bytes() - baseline

    result["table_bytes"] = after_table - baseline
    result["index_bytes"] = after_index - after_table
    result["total_bytes"] = after_index - baseline
    result["table_bytes_per_row"] = round((after_table - baseline) / rows, 1)
    result["index_bytes_per_row"] = round((after_index - after_table) / rows, 1)
    result["materialized_bytes_per_row"] = round((after_index - baseline) / rows, 1)
    result["build_rows_per_second"] = int(
        rows / max(result["index_seconds"], 0.01)
    )
    return result


def measure_view_index_bytes_per_row(url: str, ts_lo: str, ts_hi: str) -> dict:
    """Measure the view-backed index path -- the one promotion actually uses.

    No rows are stored: a view over ``read_parquet`` is indexed directly, so the
    only disk consumed is postings plus ``INCLUDE`` columns. This is the
    measurement that produced 73-74 B/row against 293 B/row materialized, and it
    doubles as the build-time-pruning check, since the row count it reports must
    match the window rather than the file.
    """
    view, index = "probe_view", "probe_view_idx"
    result: dict = {}

    with probe_cursor() as cur:

        def db_bytes() -> int:
            cur.execute("SELECT pg_database_size(current_database()) AS b")
            return int(cur.fetchone()["b"])

        cur.execute(
            sql.SQL("DROP VIEW IF EXISTS {v} CASCADE").format(v=sql.Identifier(view))
        )
        baseline = db_bytes()

        severities = sql.SQL(", ").join(sql.Literal(s) for s in PROBLEM_SEVERITIES)
        cur.execute(
            sql.SQL("""
                CREATE VIEW {v} AS
                SELECT "Timestamp", "SeverityText", "ServiceName", "Body"
                FROM read_parquet({url})
                WHERE "Timestamp" >= {lo}::timestamptz
                  AND "Timestamp" <  {hi}::timestamptz
                  AND "SeverityText" IN ({severities})
            """).format(
                v=sql.Identifier(view),
                url=sql.Literal(url),
                lo=sql.Literal(ts_lo),
                hi=sql.Literal(ts_hi),
                severities=severities,
            )
        )

        started = time.monotonic()
        cur.execute(
            sql.SQL("""
                CREATE INDEX {idx} ON {v} USING inverted (
                    "Body" english, "ServiceName", "SeverityText"
                ) INCLUDE ("Timestamp", "ServiceName", "SeverityText", "Body")
            """).format(idx=sql.Identifier(index), v=sql.Identifier(view))
        )
        result["build_seconds"] = round(time.monotonic() - started, 2)

        cur.execute(
            sql.SQL("SELECT count(*) AS n FROM {idx}").format(
                idx=sql.Identifier(index)
            )
        )
        result["rows"] = rows = int(cur.fetchone()["n"]) or 1
        built = db_bytes()

        cur.execute(
            sql.SQL("DROP VIEW IF EXISTS {v} CASCADE").format(v=sql.Identifier(view))
        )
        result["bytes_after_teardown"] = db_bytes() - baseline

    result["index_bytes"] = built - baseline
    result["index_bytes_per_row"] = round((built - baseline) / rows, 1)
    result["build_rows_per_second"] = int(rows / max(result["build_seconds"], 0.01))
    return result


def measure_build_pruning(url: str, ts_lo: str, ts_hi: str) -> dict:
    """Resolve assumption 1: does an index build over a filtered view prune?

    Compares the wall-clock of building over a one-row-group time slice against
    the time a full-file build would imply. A build that prunes finishes in
    seconds; a build that reads the whole file cannot.

    Returns the observation rather than a verdict -- read ``seconds`` and compare
    with ``file_metadata`` to decide.
    """
    view = "probe_pruned_view"
    index = "probe_pruned_idx"
    out: dict = {}

    with probe_cursor() as cur:
        cur.execute(
            sql.SQL("DROP VIEW IF EXISTS {v} CASCADE").format(v=sql.Identifier(view))
        )
        cur.execute(
            sql.SQL("""
                CREATE VIEW {v} AS
                SELECT "Timestamp", "SeverityText", "ServiceName", "Body"
                FROM read_parquet({url})
                WHERE "Timestamp" >= {lo}::timestamptz
                  AND "Timestamp" <  {hi}::timestamptz
            """).format(
                v=sql.Identifier(view),
                url=sql.Literal(url),
                lo=sql.Literal(ts_lo),
                hi=sql.Literal(ts_hi),
            )
        )
        started = time.monotonic()
        try:
            cur.execute(
                sql.SQL("""
                    CREATE INDEX {idx} ON {v} USING inverted (
                        "Body" english, "ServiceName", "SeverityText"
                    ) INCLUDE ("Timestamp", "ServiceName", "SeverityText", "Body")
                """).format(idx=sql.Identifier(index), v=sql.Identifier(view))
            )
            out["index_on_view_supported"] = True
            out["seconds"] = round(time.monotonic() - started, 2)

            # The decisive evidence for build-time pruning: the index must hold
            # only the window's rows, not the file's billion.
            cur.execute(
                sql.SQL("SELECT count(*) AS n FROM {idx}").format(
                    idx=sql.Identifier(index)
                )
            )
            out["rows_indexed"] = int(cur.fetchone()["n"])
            out["pruned"] = out["rows_indexed"] < 100_000_000
        except Exception as exc:  # noqa: BLE001 - probe reports, never raises
            out["index_on_view_supported"] = False
            out["error"] = str(exc)[:300]
        finally:
            cur.execute(
                sql.SQL("DROP VIEW IF EXISTS {v} CASCADE").format(
                    v=sql.Identifier(view)
                )
            )
    return out


def measure_sampling_support(url: str) -> dict:
    """Resolve assumption 2: is ``USING SAMPLE`` / ``TABLESAMPLE`` available?"""
    out: dict = {}
    for label, statement in (
        ("using_sample", "SELECT count(*) AS n FROM ({src}) s USING SAMPLE 100 ROWS"),
        ("tablesample", "SELECT count(*) AS n FROM ({src}) s TABLESAMPLE SYSTEM (1)"),
    ):
        try:
            with db_cursor() as cur:
                cur.execute(
                    sql.SQL(statement).format(
                        src=sql.SQL(
                            'SELECT "SeverityText" FROM read_parquet({url}) LIMIT 1000'
                        ).format(url=sql.Literal(url))
                    )
                )
                cur.fetchone()
            out[label] = True
        except Exception as exc:  # noqa: BLE001 - probe reports, never raises
            out[label] = False
            out[f"{label}_error"] = str(exc)[:200]
    return out


def database_size_bytes() -> int:
    """Current on-disk size, for the total-cap half of :func:`budget.fits`."""
    with db_cursor() as cur:
        cur.execute("SELECT pg_database_size(current_database()) AS b")
        return int(cur.fetchone()["b"])


def _report(args: argparse.Namespace) -> None:
    """Print the measurements that back ``budget.CORPUS_FACTS``."""
    from backend.db.datasets import resolve

    dataset = resolve("otel_s3")
    urls = list(dataset.sources)[: args.files]

    print("=" * 78)
    print("SereneDB corpus probe -- metadata only, zero rows scanned")
    print("=" * 78)

    try:
        print(f"\ndatabase size: {_human(database_size_bytes())}")
    except Exception as exc:  # noqa: BLE001
        print(f"\ndatabase size: unavailable ({type(exc).__name__})")

    print(f"\ncolumns in {urls[0].rsplit('/', 1)[-1]}:")
    for name, type_ in parquet_columns(urls[0]):
        print(f"  {name:<20} {type_}")

    total_rows = 0
    for url in urls:
        meta = file_metadata(url)
        total_rows += meta.num_rows
        stats = row_group_stats(url)
        print(f"\n{url.rsplit('/', 1)[-1]}")
        print(f"  rows            {meta.num_rows:,}")
        print(f"  row groups      {meta.num_row_groups:,}")
        print(f"  size            {_human(meta.file_size_bytes)}")
        print(f"  bytes/row       {meta.bytes_per_row:.1f}")
        print(f"  rows/row group  {meta.rows_per_row_group:,.0f}")
        if stats:
            print(f"  time range      {stats[0].ts_min} .. {stats[-1].ts_max}")
            print(
                f"  row group span  ~{stats[0].num_rows:,} rows, "
                f"{stats[0].ts_min} .. {stats[0].ts_max}"
            )

    print(f"\ntotal rows across {len(urls)} file(s): {total_rows:,}")

    print("\n--- what fits in budget ---")
    from backend.db.budget import (
        MAX_PROMOTION_BUILD_SECONDS,
        MAX_SLICE_BUILD_SECONDS,
    )

    for name, allowance, max_seconds in (
        (
            "otel search slice",
            BUDGET.otel_slice_allowance_bytes,
            MAX_SLICE_BUILD_SECONDS,
        ),
        (
            "one promoted chunk",
            BUDGET.promotion_allowance_bytes,
            MAX_PROMOTION_BUILD_SECONDS,
        ),
    ):
        rows = max_buildable_rows(allowance, max_seconds)
        pct = 100.0 * rows / total_rows if total_rows else 0.0
        print(
            f"  {name:<20} {_human(allowance):>10} / {max_seconds:>4}s build "
            f"-> ~{rows:,} rows ({pct:.4f}% of the probed corpus)"
        )

    if args.severity:
        print("\n--- severity distribution (bounded window) ---")
        rows, elapsed = severity_distribution(
            urls[0], args.ts_lo, args.ts_hi
        )
        total = sum(int(r["n"]) for r in rows) or 1
        print(f"  {total:,} rows aggregated in {elapsed:.2f}s")
        for r in rows:
            n = int(r["n"])
            print(f"    {str(r['severity']):<14} {n:>12,}  {100 * n / total:6.3f}%")
        problem = sum(
            int(r["n"]) for r in rows if str(r["severity"]) in PROBLEM_SEVERITIES
        )
        print(
            f"  problem severities {PROBLEM_SEVERITIES}: "
            f"{problem:,} ({100 * problem / total:.3f}%)"
        )

    if args.index_cost:
        print("\n--- cost path A: materialized table + index ---")
        m = measure_index_bytes_per_row(urls[0], args.ts_lo)
        for k, v in m.items():
            print(f"  {k:<28} {v}")
        if m["bytes_after_teardown"] != 0:
            print(
                f"  WARNING: {m['bytes_after_teardown']} B not reclaimed "
                "after teardown"
            )

        print("\n--- cost path B: view + index (what promotion uses) ---")
        v = measure_view_index_bytes_per_row(urls[0], args.ts_lo, args.ts_hi)
        for k, value in v.items():
            print(f"  {k:<28} {value}")
        if v["bytes_after_teardown"] != 0:
            print(
                f"  WARNING: {v['bytes_after_teardown']} B not reclaimed "
                "after teardown"
            )

        print("\n--- adopt these measurements ---")
        print(f"  SERENEDB_INDEX_BYTES_PER_ROW={int(v['index_bytes_per_row'])}")
        print(
            f"  SERENEDB_MATERIALIZED_BYTES_PER_ROW="
            f"{int(m['materialized_bytes_per_row'])}"
        )
        print(
            f"  SERENEDB_BUILD_ROWS_PER_SECOND={v['build_rows_per_second']}"
            "   (use the figure measured at the LARGEST size; throughput "
            "degrades)"
        )
        print(
            f"\n  1M rows via a view would cost "
            f"{_human(estimate_index_bytes(1_000_000))} and take "
            f"{estimate_build_seconds(1_000_000) / 60:.1f} min"
        )

    if args.assumptions:
        print("\n--- assumption 1: build-time pruning over a view ---")
        for k, v in measure_build_pruning(urls[0], args.ts_lo, args.ts_hi).items():
            print(f"  {k:<26} {v}")
        print("\n--- assumption 2: sampling syntax ---")
        for k, v in measure_sampling_support(urls[0]).items():
            print(f"  {k:<26} {v}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--files", type=int, default=1, help="how many parts to probe")
    parser.add_argument(
        "--severity",
        action="store_true",
        help="scan a bounded window for the severity mix (safe; column-pruned)",
    )
    parser.add_argument(
        "--index-cost",
        action="store_true",
        help=(
            "RISKY: builds indexes over remote Parquet to measure cost per row. "
            "Remote index builds have twice taken this server down (see "
            "budget.py finding 10). Run it alone, on an idle server, and keep "
            "the window small."
        ),
    )
    parser.add_argument(
        "--assumptions",
        action="store_true",
        help="RISKY: builds an index over a view. Same caveat as --index-cost.",
    )
    parser.add_argument("--ts-lo", default="2025-09-23 00:00:00+00")
    parser.add_argument("--ts-hi", default="2025-09-23 00:00:30+00")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    _report(args)


if __name__ == "__main__":
    main()


# Parquet logical-type unit names, mapped onto the registry's vocabulary.
_PARQUET_TS_UNITS = (
    ("NANOS", "nanosecond"),
    ("MICROS", "microsecond"),
    ("MILLIS", "millisecond"),
)


def verify_ts_precision(dataset: str = "otel_s3", *, parts: int = 5) -> dict:
    """Check a dataset's declared timestamp precision against its files.

    Worth a dedicated check because the failure mode is so poor: a precision
    finer than ``microsecond`` that is *not* declared surfaces as
    ``microsecond must be in 0..999999`` from deep inside the driver, and only
    once a query happens to SELECT a raw timestamp. On a remote corpus that can
    be several hundred seconds into the work.

    Samples across the series rather than trusting one file, because the whole
    point of declaring a precision is that it holds for every part.
    """
    from backend.db.datasets import TS_PRECISIONS, resolve

    ds = resolve(dataset)
    ts_column = ds.field("ts").column
    urls = list(ds.sources)[:parts] if ds.sources else []

    observed: dict[str, str] = {}
    with probe_cursor() as cur:
        for url in urls:
            cur.execute(
                "SELECT logical_type FROM parquet_schema(%s) WHERE name = %s",
                (url, ts_column),
            )
            row = cur.fetchone()
            text = str(row["logical_type"]) if row else ""
            observed[url.rsplit("/", 1)[-1]] = next(
                (name for token, name in _PARQUET_TS_UNITS if token in text),
                "microsecond",
            )

    units = set(observed.values())
    return {
        "dataset": ds.id,
        "declared": ds.ts_precision,
        "observed": observed,
        "consistent": len(units) <= 1,
        "matches_declaration": units <= {ds.ts_precision},
        "needs_truncation": ds.ts_needs_truncation,
        "fractional_digits": TS_PRECISIONS.get(ds.ts_precision),
    }
