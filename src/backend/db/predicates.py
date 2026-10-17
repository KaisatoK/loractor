"""Shared SQL fragments, built from canonical fields.

Every builder composes its WHERE clause from these, so there is one definition
of "filter by time", "filter by severity" and "bucket by minute" rather than one
per query module. That is what lets a new corpus be a registration instead of a
new set of query builders.

Nothing here interpolates a caller-supplied string into SQL: identifiers come
from the registry, values go through placeholders, and the only raw SQL is the
code-owned expressions in :mod:`backend.db.datasets`.
"""

from __future__ import annotations

from typing import Literal

from psycopg2 import sql

from backend.db.datasets import Capability, Dataset, datasets_with
from backend.db.errors import refuse
from backend.db.windows import TimeWindow

Bucket = Literal["second", "minute", "hour", "day"]

BUCKETS: tuple[str, ...] = ("second", "minute", "hour", "day")


def ts_filter(dataset: Dataset, window: TimeWindow) -> tuple[sql.Composable, list]:
    """``ts >= start AND ts < end``, cast the way the dataset needs.

    Half-open so adjacent windows tile without double-counting a boundary row.
    The cast comes from the registry: the OpenStack column is a naive
    ``timestamp`` and the OTel column is a ``timestamptz``, and using the wrong
    one either errors or silently compares against a shifted instant.
    """
    cast = sql.SQL(dataset.ts_cast)
    clause = sql.SQL("{ts} >= %s::{cast} AND {ts} < %s::{cast}").format(
        ts=dataset.sql_for("ts"), cast=cast
    )
    return clause, [window.start, window.end]


def severity_filter(
    dataset: Dataset, severity: str | None
) -> tuple[sql.Composable, list]:
    """An optional equality filter on severity, validated against the corpus.

    Severity casing is per-corpus and measured, not assumed: OpenStack uses
    ``ERROR`` and the OTel corpus uses ``error``. An unknown value is refused
    with the real vocabulary rather than silently matching nothing -- which is
    exactly the bug that filtering a lower-case corpus with ``'ERROR'`` causes.
    """
    if severity is None:
        return sql.SQL(""), []

    if not dataset.has_field("severity"):
        raise refuse(
            f"'{dataset.id}' has no severity field to filter on.",
            "Drop the severity filter for this dataset.",
            code="unsupported_field",
        )

    known = dataset.severity_values
    if severity not in known:
        matches = [v for v in known if v.lower() == severity.lower()]
        if matches:
            raise refuse(
                f"Severity '{severity}' is the wrong case for '{dataset.id}'.",
                f"Use '{matches[0]}'. This corpus' values are: "
                f"{', '.join(known)}.",
                code="severity_case",
                corrected=matches[0],
            )
        raise refuse(
            f"'{severity}' is not a severity in '{dataset.id}'.",
            f"Use one of: {', '.join(known)}.",
            code="unknown_severity",
            severity_values=list(known),
        )

    return sql.SQL("{col} = %s").format(col=dataset.sql_for("severity")), [severity]


def field_equals(
    dataset: Dataset, canonical: str, value: object | None
) -> tuple[sql.Composable, list]:
    """An optional equality filter on any canonical field."""
    if value is None:
        return sql.SQL(""), []
    return sql.SQL("{col} = %s").format(col=dataset.sql_for(canonical)), [value]


def where(*clauses: tuple[sql.Composable, list]) -> tuple[sql.Composable, list]:
    """Join non-empty clauses with AND into a complete WHERE clause."""
    parts: list[sql.Composable] = []
    params: list = []
    for clause, clause_params in clauses:
        rendered = clause
        if isinstance(clause, sql.SQL) and not clause.string:
            continue
        parts.append(rendered)
        params.extend(clause_params)

    if not parts:
        return sql.SQL(""), []
    return sql.SQL("WHERE ") + sql.SQL(" AND ").join(parts), params


def bounded_limit(dataset: Dataset, limit: int | None) -> sql.Composable:
    """A mandatory ``LIMIT``, clamped to the dataset's ceiling.

    There is no unbounded path: ``None`` becomes the dataset's maximum rather
        than no limit at all. The original ``SELECT DISTINCT`` with no limit is
    harmless on 2,000 rows and fatal on a billion.
    """
    effective = dataset.max_rows if limit is None else min(limit, dataset.max_rows)
    if effective < 1:
        raise refuse(
            f"A limit of {limit} returns nothing.",
            f"Ask for between 1 and {dataset.max_rows} rows.",
            code="bad_limit",
        )
    return sql.SQL("LIMIT {n}").format(n=sql.Literal(effective))


def bucket_expression(dataset: Dataset, bucket: str) -> sql.Composable:
    """``date_trunc`` on the canonical timestamp.

    Replaces the original ``EXTRACT(MINUTE FROM log_time)``, which collapsed
    every hour onto the same sixty buckets and so was only meaningful inside a
    single hour -- and which referenced a column the OTel corpus does not have.
    """
    if bucket not in BUCKETS:
        raise refuse(
            f"'{bucket}' is not a time bucket.",
            f"Use one of: {', '.join(BUCKETS)}.",
            code="bad_bucket",
        )
    return sql.SQL("date_trunc({unit}, {ts})").format(
        unit=sql.Literal(bucket), ts=dataset.sql_for("ts")
    )


def require(dataset: Dataset, capability: Capability, what: str) -> None:
    """Refuse up front when a dataset cannot do what is being asked.

    Capability gating is declarative so an absent feature produces an
    actionable refusal instead of a SQL error about a missing relation. The
    OTel corpus having no primary key is the motivating case.
    """
    if not dataset.supports(capability):
        able = [d.id for d in datasets_with(capability)]
        raise refuse(
            f"'{dataset.id}' does not support {what}.",
            (
                f"Use one of these datasets instead: {', '.join(able)}."
                if able
                else "No registered dataset supports it."
            ),
            code="unsupported_capability",
            capability=str(capability),
        )


def count_star() -> sql.Composable:
    return sql.SQL("count(*)")


def approx_distinct(dataset: Dataset, canonical: str) -> sql.Composable:
    """``approx_count_distinct`` for the remote tier, exact otherwise.

    Distinct counts over a billion rows are the aggregate that most needs
    approximating; the exact version builds a hash table over every value.
    Whichever is used, the dataset's ``EXACT_COUNTS`` capability decides whether
    the result gets labelled approximate.
    """
    column = dataset.sql_for(canonical)
    if dataset.exact_counts:
        return sql.SQL("count(DISTINCT {col})").format(col=column)
    return sql.SQL("approx_count_distinct({col})").format(col=column)
