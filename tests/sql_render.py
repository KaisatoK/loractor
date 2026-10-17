"""Render a psycopg2 ``sql.Composable`` to text without a database connection.

``Composable.as_string`` requires a real connection or cursor, so SQL-construction
tests would otherwise need a live SereneDB. This renderer walks the composition
tree instead, which keeps every SQL-shape assertion offline and deterministic.

It is a test helper, not a query path: nothing here is ever executed. Literal
quoting is good enough to assert on, not good enough to run.
"""

from __future__ import annotations

from psycopg2 import sql


def render(composable: sql.Composable) -> str:
    """Approximate the SQL text ``composable`` would produce."""
    if isinstance(composable, sql.Composed):
        return "".join(render(part) for part in composable.seq)
    if isinstance(composable, sql.Identifier):
        return ".".join(f'"{s}"' for s in composable.strings)
    if isinstance(composable, sql.Placeholder):
        return "%s"
    if isinstance(composable, sql.Literal):
        return _literal(composable.wrapped)
    if isinstance(composable, sql.SQL):
        return composable.string
    raise TypeError(f"cannot render {type(composable).__name__}")


def _literal(value: object) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return str(value)
    escaped = str(value).replace("'", "''")
    return f"'{escaped}'"


def normalized(composable: sql.Composable) -> str:
    """Rendered SQL collapsed to single spaces, for whitespace-blind asserts."""
    return " ".join(render(composable).split())
