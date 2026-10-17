"""The dataset catalog: what each corpus holds, measured on every call.

Every count, trend and ranking tool requires a time window, so an agent that
does not know when a corpus starts and ends has to guess one -- and a guessed
window either misses the data entirely or straddles it in a way that makes a
"first half vs second half" comparison meaningless. This module answers the
question the window depends on: for each registered dataset, how many rows,
from when to when, at which severities.

Nothing here is cached and nothing is hard-coded. The facts are read from the
data each time, by the cheapest means the dataset's kind allows:

=================  =========================================================
native table       one ``GROUP BY severity`` over every row: exact row count,
                   per-level counts, first and last timestamp
remote Parquet     the files' footers only (row counts and per-row-group
                   timestamp min/max); no row is read, so severity levels are
                   the declared vocabulary and their counts are unknown
in-place index     the window and severities the index was **built over**,
                   recorded at build time, plus the index's own row count
=================  =========================================================

A dataset that cannot be profiled -- a local table that was never loaded, an
index that was never built, a footer read that failed -- comes back as a row
with ``available: false`` and a note saying why, so one missing corpus never
hides the others.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timedelta

from psycopg2 import sql

from backend.db import external
from backend.db.connection import query_runner
from backend.db.datasets import Dataset, DatasetKind, all_datasets
from backend.db.pruning import _to_datetime, load_row_groups
from backend.db.stats import QueryResult, QueryStats
from backend.db.windows import max_window_seconds

logger = logging.getLogger(__name__)

# Dataset kinds whose rows live in a local table and can be aggregated exactly.
_LOCAL_KINDS = frozenset({DatasetKind.NATIVE_TABLE, DatasetKind.LOCAL_FILE})

_WINDOW_FORMAT = "%Y-%m-%d %H:%M:%S"


# --- Query builders -----------------------------------------------------------
@query_runner
def existing_relations(names: tuple[str, ...]) -> tuple[sql.Composable, list]:
    """Which of ``names`` exist as tables or views.

    Checked up front so an unloaded corpus is reported as unavailable instead of
    surfacing as a failed query.
    """
    query = sql.SQL(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_name IN ({names})"
    ).format(names=sql.SQL(", ").join(sql.Placeholder() for _ in names))
    return query, list(names)


@query_runner
def level_profile(dataset_id: str) -> tuple[sql.Composable, list, QueryStats]:
    """Exact row count, first and last timestamp per severity level.

    One aggregate over every row of a local table. It reads only the timestamp
    and severity columns, which on the 4.7M-row BGL corpus measured 0.02s.
    """
    from backend.db.datasets import resolve

    ds = resolve(dataset_id)
    ts = ds.sql_for("ts")
    columns = [
        sql.SQL("count(*) AS count"),
        sql.SQL("min({ts}) AS first_ts").format(ts=ts),
        sql.SQL("max({ts}) AS last_ts").format(ts=ts),
    ]
    group: sql.Composable = sql.SQL("")
    if ds.has_field("severity"):
        severity = ds.sql_for("severity")
        columns.insert(0, sql.SQL("{col} AS severity").format(col=severity))
        group = sql.SQL("GROUP BY {col}").format(col=severity)

    query = sql.SQL("""
        SELECT {columns}
        FROM {relation}
        {group}
        ORDER BY count DESC
    """).format(
        columns=sql.SQL(", ").join(columns),
        relation=ds.scan_relation(),
        group=group,
    )
    return query, [], QueryStats(dataset=ds.id, approximate=False)


@query_runner
def slice_metadata() -> tuple[sql.Composable, list]:
    """The build record the in-place index wrote when it was created."""
    query = sql.SQL(
        "SELECT window_start, window_end, severities, indexed_rows, built_at "
        "FROM {meta} ORDER BY built_at DESC LIMIT 1"
    ).format(meta=sql.Identifier(external.META_TABLE))
    return query, []


@query_runner
def slice_view_definition() -> tuple[sql.Composable, list]:
    """The index view's SQL, for an index built before the build record existed."""
    query = sql.SQL(
        "SELECT view_definition FROM information_schema.views "
        "WHERE table_name = %s"
    )
    return query, [external.VIEW_NAME]


@query_runner
def index_row_count(dataset_id: str) -> tuple[sql.Composable, list]:
    """Rows the in-place index holds, counted through the index itself."""
    from backend.db.datasets import resolve

    ds = resolve(dataset_id)
    query = sql.SQL("SELECT count(*) AS n FROM {idx}").format(
        idx=ds.search_relation()
    )
    return query, []


# --- Profiles -------------------------------------------------------------------
def _window_text(moment: datetime, dataset: Dataset) -> str:
    """A bound in the form the windowed tools accept for this dataset.

    Local corpora take a naive UTC time (every session is pinned to UTC); the
    remote tiers are documented with an explicit '+00' offset.
    """
    text = moment.strftime(_WINDOW_FORMAT)
    return text if dataset.kind in _LOCAL_KINDS else f"{text}+00"


def _span(
    dataset: Dataset, first: datetime | None, last: datetime | None, *, end_exclusive: bool
) -> dict:
    """Span facts plus a ready-to-use whole-dataset window.

    ``window_end`` is one second past the last row (whole seconds, exclusive),
    so a query over ``window_start``..``window_end`` includes every row; for a
    build window the end is already exclusive and is used as is.
    """
    ceiling = max_window_seconds(dataset)
    if first is None or last is None:
        return {
            "first_timestamp": None,
            "last_timestamp": None,
            "span_seconds": None,
            "max_window_seconds": ceiling,
            "whole_span_fits_one_window": None,
            "window_start": None,
            "window_end": None,
        }
    start = first.replace(microsecond=0)
    end = last if end_exclusive else last.replace(microsecond=0) + timedelta(seconds=1)
    window_seconds = (end - start).total_seconds()
    return {
        "first_timestamp": first,
        "last_timestamp": last,
        "span_seconds": round((last - first).total_seconds(), 3),
        "max_window_seconds": ceiling,
        "whole_span_fits_one_window": window_seconds <= ceiling,
        "window_start": _window_text(start, dataset),
        "window_end": _window_text(end, dataset),
    }


def _base(dataset: Dataset) -> dict:
    return {
        "dataset": dataset.id,
        "kind": str(dataset.kind),
        "exact_counts": dataset.exact_counts,
        "available": True,
    }


def unavailable(dataset: Dataset, note: str) -> dict:
    """A catalog row for a dataset that could not be profiled."""
    return {
        **_base(dataset),
        "available": False,
        "rows": None,
        "rows_exact": None,
        **_span(dataset, None, None, end_exclusive=False),
        "levels": [],
        "timestamp_source": None,
        "note": note,
    }


def summarize_levels(dataset: Dataset, rows: list[dict]) -> dict:
    """Fold per-level aggregate rows into one exact catalog row."""
    total = sum(int(r["count"]) for r in rows)
    firsts = [r["first_ts"] for r in rows if r.get("first_ts") is not None]
    lasts = [r["last_ts"] for r in rows if r.get("last_ts") is not None]
    levels = [
        {"severity": r.get("severity"), "count": int(r["count"])}
        for r in sorted(rows, key=lambda r: -int(r["count"]))
    ]
    note = (
        "Exact: counted over every row. Each level's count is the number of log "
        "entries at that severity in the whole dataset."
    )
    if total == 0:
        note = "The dataset is loaded but holds no rows."
    return {
        **_base(dataset),
        "rows": total,
        "rows_exact": True,
        **_span(
            dataset,
            min(firsts) if firsts else None,
            max(lasts) if lasts else None,
            end_exclusive=False,
        ),
        "levels": levels,
        "timestamp_source": "rows",
        "note": note,
    }


def local_profile(dataset: Dataset) -> tuple[dict, int]:
    """Catalog row for a local table, and the rows its aggregate scanned."""
    result = level_profile(dataset.id)
    row = summarize_levels(dataset, list(result))
    return row, row["rows"] or 0


def remote_profile(dataset: Dataset, *, fetch=None) -> dict:
    """Catalog row for a remote Parquet corpus, from footers alone.

    Footer row counts are exact and so are the timestamp statistics, so rows and
    span are facts; but no row is read, so the severity levels are the
    dataset's declared vocabulary with unknown counts.
    """
    ts_column = dataset.field("ts").column
    groups = [
        group
        for url in dataset.sources
        for group in load_row_groups(url, ts_column, fetch=fetch)
    ]
    if not groups:
        return unavailable(dataset, "The files' footers list no row groups.")
    return {
        **_base(dataset),
        "rows": sum(g.num_rows for g in groups),
        "rows_exact": True,
        **_span(
            dataset,
            min(g.ts_min for g in groups),
            max(g.ts_max for g in groups),
            end_exclusive=False,
        ),
        "levels": [
            {"severity": value, "count": None} for value in dataset.severity_values
        ],
        "timestamp_source": "parquet_footers",
        "note": (
            f"Rows and span from the footers of {len(dataset.sources)} file(s) "
            f"({len(groups):,} row groups); no row was read. Levels are the "
            "declared vocabulary, NOT counted: count them with a windowed "
            "aggregate. Aggregates on this dataset are approximate."
        ),
    }


_TS_LITERAL = re.compile(
    r"'(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[+-]\d{2}(?::?\d{2})?|Z)?)'"
)
_IN_LIST = re.compile(r"\bIN\s*\(([^)]*)\)", re.IGNORECASE)


def parse_view_window(definition: str) -> tuple[str, str, tuple[str, ...]] | None:
    """(start, end, severities) from the index view's WHERE clause, if legible.

    The view is created by :func:`backend.db.external.build_external_index` with
    ``ts >= start AND ts < end AND severity IN (...)``, in that order.
    """
    stamps = _TS_LITERAL.findall(definition or "")
    if len(stamps) < 2:
        return None
    match = _IN_LIST.search(definition)
    severities = (
        tuple(re.findall(r"'([^']*)'", match.group(1))) if match else ()
    )
    return stamps[0], stamps[1], severities


def _slice_row(
    dataset: Dataset,
    start: str,
    end: str,
    severities: tuple[str, ...],
    rows: int | None,
    source: str,
) -> dict:
    return {
        **_base(dataset),
        "rows": rows,
        "rows_exact": rows is not None,
        **_span(dataset, _to_datetime(start), _to_datetime(end), end_exclusive=True),
        "levels": [{"severity": value, "count": None} for value in severities],
        "timestamp_source": source,
        "note": (
            "The timestamps are the window the index was BUILT over (end "
            "exclusive), not observed first/last rows; levels are the "
            "severities it was built with, not counted. Text search here "
            "covers that window and those severities only."
        ),
    }


def slice_profile(dataset: Dataset, present: set[str]) -> dict:
    """Catalog row for the in-place index: the window it was built over."""
    if external.VIEW_NAME not in present:
        return unavailable(
            dataset,
            "The in-place index has not been built, so there is nothing to "
            "search on this dataset.",
        )

    rows = None
    try:
        counted = index_row_count(dataset.id)
        rows = int(counted[0]["n"]) if counted else None
    except Exception:  # noqa: BLE001 -- a missing count must not hide the window
        logger.warning("Could not count %s", dataset.id, exc_info=True)

    if external.META_TABLE in present:
        record = slice_metadata()
        if record:
            meta = record[0]
            severities = tuple(
                s for s in str(meta.get("severities") or "").split(",") if s
            )
            return _slice_row(
                dataset,
                str(meta["window_start"]),
                str(meta["window_end"]),
                severities,
                rows,
                "index_build_record",
            )

    definition = slice_view_definition()
    parsed = parse_view_window(definition[0]["view_definition"]) if definition else None
    if parsed is None:
        row = unavailable(
            dataset,
            "The index exists, but the window it was built over is not "
            "recorded; rebuild it to record one.",
        )
        row.update(available=True, rows=rows, rows_exact=rows is not None)
        return row
    start, end, severities = parsed
    return _slice_row(dataset, start, end, severities, rows, "index_view_definition")


# --- The catalog ------------------------------------------------------------------
def _relation_for(dataset: Dataset) -> str | None:
    """The relation whose existence decides whether ``dataset`` is available."""
    if dataset.kind in _LOCAL_KINDS:
        return dataset.sources[0]
    if dataset.kind is DatasetKind.EXTERNAL_INDEX:
        return external.VIEW_NAME
    return None


def list_datasets(*, fetch_row_groups=None) -> QueryResult:
    """One catalog row per registered dataset, measured now.

    Raises only when *no* dataset could be profiled (the database is down), so
    a failed catalog is reported as a failure rather than as an empty one.
    """
    started = time.monotonic()
    datasets = all_datasets()
    names = tuple(
        sorted(
            {n for n in (_relation_for(d) for d in datasets) if n}
            | {external.META_TABLE}
        )
    )
    present = {r["table_name"] for r in existing_relations(names)}

    rows: list[dict] = []
    scanned = 0
    failures = 0
    for dataset in datasets:
        try:
            relation = _relation_for(dataset)
            if dataset.kind in _LOCAL_KINDS:
                if relation not in present:
                    rows.append(
                        unavailable(
                            dataset,
                            "Not loaded: the dataset's data is not in the "
                            "database. Load it with `bash ./setup.sh`.",
                        )
                    )
                    continue
                row, read = local_profile(dataset)
                scanned += read
            elif dataset.kind is DatasetKind.REMOTE_PARQUET:
                row = remote_profile(dataset, fetch=fetch_row_groups)
            else:
                row = slice_profile(dataset, present)
            rows.append(row)
        except Exception as error:  # noqa: BLE001 -- one corpus must not hide the rest
            failures += 1
            logger.warning("Could not profile %s", dataset.id, exc_info=True)
            rows.append(
                unavailable(
                    dataset, f"Could not be profiled ({type(error).__name__})."
                )
            )

    if datasets and failures == len(datasets):
        raise RuntimeError("no dataset could be profiled")

    stats = QueryStats(
        dataset=",".join(d.id for d in datasets),
        approximate=False,
        est_rows_scanned=scanned,
        elapsed_ms=int((time.monotonic() - started) * 1000),
    )
    stats.notes.append(
        "local datasets counted over every row; remote rows and span from "
        "file footers; the in-place index from its build record"
    )
    return QueryResult(rows, stats)
