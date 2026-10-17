"""Counting queries, built from canonical fields.

The original four near-identical functions (``count_by_category``,
``count_pid_by_category``, ``count_service_by_category``,
``count_event_id_by_category``) differed only in which column they grouped by.
They are one function here, parameterized by a canonical field name, which is
why a new corpus needs no new counting code.

Every count is bounded: a mandatory time window, a pre-flight pruning estimate
for remote corpora, and a ``LIMIT`` on the number of groups returned. The
original versions had none of those and were safe only because the table held
2,000 rows.
"""

from __future__ import annotations

import logging

from psycopg2 import sql

from backend.db import predicates as pred
from backend.db.connection import bulk_query, query_runner
from backend.db.datasets import Dataset, DatasetKind, local_datasets, resolve
from backend.db.errors import refuse
from backend.db.pruning import plan_and_guard
from backend.db.stats import QueryStats
from backend.db.windows import build_window

logger = logging.getLogger(__name__)


def _prepare(
    dataset_id: str | None, start_time: str, end_time: str
) -> tuple[Dataset, object, QueryStats]:
    """Resolve the dataset, validate the window, and cost it before running."""
    dataset = resolve(dataset_id)
    window = build_window(dataset, start_time, end_time)
    stats = (
        plan_and_guard(dataset, window)
        if dataset.kind is DatasetKind.REMOTE_PARQUET
        else QueryStats(
            dataset=dataset.id,
            approximate=not dataset.exact_counts,
            window_seconds=window.seconds,
        )
    )
    return dataset, window, stats


@bulk_query
def count_by_field(
    start_time: str,
    end_time: str,
    group_by: str | None = None,
    dataset: str | None = None,
    severity: str | None = None,
    limit: int | None = None,
) -> tuple[sql.Composable, list, QueryStats]:
    """Count rows per severity, optionally broken down by another field.

    ``group_by`` is a canonical field name (``service``, ``entity_id``,
    ``event_id``) or ``None`` for the severity-only breakdown that the original
    ``count_by_category`` produced.
    """
    ds, window, stats = _prepare(dataset, start_time, end_time)
    logger.debug(
        "Counting %s on %s by %s from %s to %s",
        severity or "all severities",
        ds.id,
        group_by or "severity",
        start_time,
        end_time,
    )

    group_columns: list[sql.Composable] = []
    select_columns: list[sql.Composable] = []

    if group_by:
        column = ds.sql_for(group_by)
        group_columns.append(column)
        select_columns.append(
            sql.SQL("{col} AS {alias}").format(
                col=column, alias=sql.Identifier(group_by)
            )
        )

    if ds.has_field("severity"):
        severity_column = ds.sql_for("severity")
        group_columns.append(severity_column)
        select_columns.append(
            sql.SQL("{col} AS severity").format(col=severity_column)
        )

    clause, params = pred.where(
        pred.ts_filter(ds, window), pred.severity_filter(ds, severity)
    )

    query = sql.SQL("""
        SELECT {select}, count(*) AS count
        FROM {relation}
        {clause}
        GROUP BY {group}
        ORDER BY count DESC
        {limit}
    """).format(
        select=sql.SQL(", ").join(select_columns),
        relation=ds.scan_relation(),
        clause=clause,
        group=sql.SQL(", ").join(group_columns),
        limit=pred.bounded_limit(ds, limit),
    )
    return query, params, stats


@bulk_query
def count_distinct_by_field(
    start_time: str,
    end_time: str,
    field: str,
    dataset: str | None = None,
    severity: str | None = None,
) -> tuple[sql.Composable, list, QueryStats]:
    """How many distinct values a field takes in the window.

    Uses ``approx_count_distinct`` on corpora that do not promise exact counts,
    because the exact version builds a hash table over every value in the
    window -- the aggregate that most needs approximating at this scale.
    """
    ds, window, stats = _prepare(dataset, start_time, end_time)
    clause, params = pred.where(
        pred.ts_filter(ds, window), pred.severity_filter(ds, severity)
    )

    query = sql.SQL("""
        SELECT {agg} AS distinct_count, count(*) AS total_rows
        FROM {relation}
        {clause}
    """).format(
        agg=pred.approx_distinct(ds, field),
        relation=ds.scan_relation(),
        clause=clause,
    )
    stats.approximate = stats.approximate or not ds.exact_counts
    if not ds.exact_counts:
        stats.notes.append(f"distinct count of {field} is approximate")
    return query, params, stats


@bulk_query
def top_by_field(
    start_time: str,
    end_time: str,
    field: str = "service",
    dataset: str | None = None,
    severity: str | None = None,
    limit: int = 5,
) -> tuple[sql.Composable, list, QueryStats]:
    """Rank a field's values by row volume in the window.

    Replaces ``top_in_category_by_service``, generalized off ``service``.
    """
    ds, window, stats = _prepare(dataset, start_time, end_time)
    clause, params = pred.where(
        pred.ts_filter(ds, window), pred.severity_filter(ds, severity)
    )

    query = sql.SQL("""
        SELECT {col} AS {alias}, count(*) AS count
        FROM {relation}
        {clause}
        GROUP BY {col}
        ORDER BY count DESC
        {limit}
    """).format(
        col=ds.sql_for(field),
        alias=sql.Identifier(field),
        relation=ds.scan_relation(),
        clause=clause,
        limit=pred.bounded_limit(ds, limit),
    )
    return query, params, stats


@bulk_query
def compare_periods(
    baseline_start: str,
    baseline_end: str,
    recent_start: str,
    recent_end: str,
    dataset: str | None = None,
    field: str = "service",
    limit: int = 5,
) -> tuple[sql.Composable, list, QueryStats]:
    """Compare per-field, per-severity counts between two windows.

    Both windows are validated and costed; the reported estimate is their sum,
    because both are scanned.
    """
    ds = resolve(dataset)
    baseline = build_window(ds, baseline_start, baseline_end)
    recent = build_window(ds, recent_start, recent_end)

    stats = QueryStats(dataset=ds.id, approximate=not ds.exact_counts)
    if ds.kind is DatasetKind.REMOTE_PARQUET:
        first = plan_and_guard(ds, baseline)
        second = plan_and_guard(ds, recent)
        stats = first
        stats.files_pruned += second.files_pruned
        stats.row_groups_read += second.row_groups_read
        stats.row_groups_considered += second.row_groups_considered
        stats.est_rows_scanned += second.est_rows_scanned
        stats.est_bytes_read += second.est_bytes_read
    stats.window_seconds = baseline.seconds + recent.seconds

    group_field = ds.sql_for(field)
    has_severity = ds.has_field("severity")
    severity_column = ds.sql_for("severity") if has_severity else None

    def period(window) -> tuple[sql.Composable, list]:
        clause, params = pred.where(pred.ts_filter(ds, window))
        columns = [sql.SQL("{col} AS grp").format(col=group_field)]
        group = [group_field]
        if severity_column is not None:
            columns.append(sql.SQL("{col} AS sev").format(col=severity_column))
            group.append(severity_column)
        return (
            sql.SQL("""
                SELECT {columns}, count(*) AS n
                FROM {relation} {clause}
                GROUP BY {group}
            """).format(
                columns=sql.SQL(", ").join(columns),
                relation=ds.scan_relation(),
                clause=clause,
                group=sql.SQL(", ").join(group),
            ),
            params,
        )

    baseline_sql, baseline_params = period(baseline)
    recent_sql, recent_params = period(recent)

    join_keys = [sql.SQL("r.grp = b.grp")]
    select_sev: sql.Composable = sql.SQL("")
    if severity_column is not None:
        join_keys.append(sql.SQL("r.sev = b.sev"))
        select_sev = sql.SQL("COALESCE(r.sev, b.sev) AS severity,")

    query = sql.SQL("""
        WITH baseline AS ({baseline}), recent AS ({recent})
        SELECT
            COALESCE(r.grp, b.grp) AS {alias},
            {select_sev}
            COALESCE(b.n, 0) AS baseline_count,
            COALESCE(r.n, 0) AS recent_count,
            COALESCE(r.n, 0) - COALESCE(b.n, 0) AS volume_change,
            ROUND(
                (COALESCE(r.n, 0) - COALESCE(b.n, 0)) * 100.0
                / NULLIF(b.n, 0), 2
            ) AS percentage_change
        FROM recent r
        FULL OUTER JOIN baseline b ON {join}
        ORDER BY volume_change DESC
        {limit}
    """).format(
        baseline=baseline_sql,
        recent=recent_sql,
        alias=sql.Identifier(field),
        select_sev=select_sev,
        join=sql.SQL(" AND ").join(join_keys),
        limit=pred.bounded_limit(ds, limit),
    )
    return query, baseline_params + recent_params, stats


@query_runner
def count_by_dataset(
    datasets: tuple[str, ...] | None = None,
) -> tuple[sql.Composable, list, QueryStats]:
    """Log entries per severity in each whole dataset, in one statement.

    The brief's "aggregation over the whole base corpus, such as counts by
    dataset". No time window, deliberately, and therefore only on local datasets
    with exact counts (:func:`~backend.db.datasets.local_datasets`): counting
    one of them whole is milliseconds, while the remote corpus cannot be counted
    whole at all. One ``UNION ALL`` branch per dataset, so the executed SQL is a
    single statement an answer can show.

    A requested dataset that is not loaded is refused rather than skipped: a
    corpus-wide total that silently drops a corpus is wrong, not partial.
    """
    from backend.db.catalog_query import existing_relations

    countable = {d.id for d in local_datasets()}
    ids = tuple(dict.fromkeys(datasets)) if datasets else tuple(sorted(countable))
    resolved = [resolve(i) for i in ids]

    remote = [d.id for d in resolved if d.id not in countable]
    if remote:
        raise refuse(
            f"{', '.join(remote)} cannot be counted whole: only local datasets "
            "with exact counts can.",
            "Use count_by_category_tool with a time window on that dataset "
            f"instead, or count {', '.join(sorted(countable))} here.",
            code="not_countable_whole",
        )

    present = {
        r["table_name"]
        for r in existing_relations(tuple(d.sources[0] for d in resolved))
    }
    missing = [d.id for d in resolved if d.sources[0] not in present]
    if missing:
        raise refuse(
            f"Not loaded: {', '.join(missing)}.",
            "Load it with `bash ./setup.sh`, or pass `datasets` without it.",
            code="not_loaded",
        )

    branches = [
        sql.SQL(
            "SELECT {id} AS dataset, {sev} AS severity, count(*) AS count "
            "FROM {relation} GROUP BY {sev}"
        ).format(
            id=sql.Literal(d.id),
            sev=d.sql_for("severity"),
            relation=d.scan_relation(),
        )
        for d in resolved
    ]
    query = sql.SQL("""
        SELECT dataset, severity, count
        FROM ({branches}) AS counted
        ORDER BY dataset, count DESC
    """).format(branches=sql.SQL(" UNION ALL ").join(branches))
    stats = QueryStats(dataset=",".join(ids), approximate=False)
    stats.notes.append("counted over every row of each dataset; no time window")
    return query, [], stats


@query_runner
def count_by_team(
    datasets: tuple[str, ...] | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
    category: str | None = None,
) -> tuple[sql.Composable, list, QueryStats]:
    """Count local log rows by owning team and severity.

    The ownership table is joined by both corpus and component. A LEFT JOIN is
    deliberate: components absent from the fictional mapping are retained as
    ``unowned`` instead of silently disappearing from team totals. With a
    window, the same half-open bounds are applied independently to each local
    dataset; without one, every local row is counted.
    """
    from backend.db.catalog_query import existing_relations
    from backend.db.config import OWNERSHIP_TABLE, UNOWNED_TEAM

    countable = {d.id for d in local_datasets()}
    ids = tuple(dict.fromkeys(datasets)) if datasets else tuple(sorted(countable))
    resolved = [resolve(dataset_id) for dataset_id in ids]

    remote = [d.id for d in resolved if d.id not in countable]
    if remote:
        raise refuse(
            f"{', '.join(remote)} cannot be counted by team: only local "
            "datasets with exact counts are supported.",
            "Use count_by_category_tool grouped by service with a time window "
            f"for {', '.join(remote)} instead.",
            code="not_countable_by_team",
        )

    relation_names = tuple(dict.fromkeys(
        [d.sources[0] for d in resolved] + [OWNERSHIP_TABLE]
    ))
    present = {
        row["table_name"]
        for row in existing_relations(relation_names)
    }
    missing_logs = [d.id for d in resolved if d.sources[0] not in present]
    if missing_logs:
        raise refuse(
            f"Not loaded: {', '.join(missing_logs)}.",
            "Load the local corpora with `bash ./setup.sh`.",
            code="not_loaded",
        )
    if OWNERSHIP_TABLE not in present:
        raise refuse(
            f"The ownership table '{OWNERSHIP_TABLE}' is not loaded.",
            "Run `uv run python -m backend.db.ownership` and retry.",
            code="ownership_not_loaded",
        )

    eligible: list[Dataset] = []
    skipped: list[str] = []
    for dataset in resolved:
        if category is None or category in dataset.severity_values:
            eligible.append(dataset)
            continue
        case_matches = [
            value for value in dataset.severity_values
            if value.lower() == category.lower()
        ]
        if case_matches:
            raise refuse(
                f"Severity '{category}' is the wrong case for '{dataset.id}'.",
                f"Use '{case_matches[0]}'. This corpus' values are: "
                f"{', '.join(dataset.severity_values)}.",
                code="severity_case",
                corrected=case_matches[0],
            )
        skipped.append(dataset.id)

    if not eligible:
        known = sorted({value for d in resolved for value in d.severity_values})
        raise refuse(
            f"'{category}' is not present in the requested local datasets.",
            f"Use one of: {', '.join(known)}.",
            code="unknown_severity",
            severity_values=known,
        )

    branches: list[sql.Composable] = []
    params: list = []
    for dataset in eligible:
        window = None
        if start_time is not None or end_time is not None:
            window = build_window(dataset, start_time, end_time)

        service = sql.SQL("t.{column}").format(
            column=dataset.sql_for("service")
        )
        severity = sql.SQL("t.{column}").format(
            column=dataset.sql_for("severity")
        )
        team = sql.SQL("COALESCE(o.team, {unowned})").format(
            unowned=sql.Literal(UNOWNED_TEAM)
        )
        clauses: list[tuple[sql.Composable, list]] = []
        if window is not None:
            clauses.append(pred.ts_filter(dataset, window, alias="t"))
        clauses.append(pred.severity_filter(dataset, category))
        clause, branch_params = pred.where(*clauses)
        params.extend(branch_params)

        branches.append(sql.SQL(
            "SELECT {dataset} AS dataset, {team} AS team, {severity} AS severity, "
            "count(*) AS count "
            "FROM {relation} AS t "
            "LEFT JOIN {ownership} AS o ON o.dataset = {corpus} "
            "AND o.component = {service} "
            "{clause} "
            "GROUP BY {team}, {severity}"
        ).format(
            dataset=sql.Literal(dataset.id),
            team=team,
            severity=severity,
            relation=dataset.scan_relation(),
            ownership=sql.Identifier(OWNERSHIP_TABLE),
            corpus=sql.Literal(dataset.corpus_name),
            service=service,
            clause=clause,
        ))

    query = sql.SQL(
        "SELECT dataset, team, severity, count "
        "FROM ({branches}) AS counted "
        "ORDER BY count DESC, dataset, team, severity"
    ).format(branches=sql.SQL(" UNION ALL ").join(branches))
    stats = QueryStats(dataset=",".join(ids), approximate=False)
    if start_time is None and end_time is None:
        stats.notes.append("counted over every row of each requested local dataset")
    else:
        stats.notes.append("counted in the requested half-open time window")
    if skipped:
        stats.notes.append(
            f"Skipped datasets without severity '{category}': {', '.join(skipped)}"
        )
    return query, params, stats
