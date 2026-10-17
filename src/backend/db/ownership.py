"""The fictional team-ownership table: read and validate the CSV, load it.

``src/backend/data/team_ownership.csv`` maps ``(dataset, component, team)`` so
team-lead and manager answers can name owning teams. ``component`` matches the
registry's ``service`` field (OpenStack and BGL ``Component``) exactly; components
not listed count as ``UNOWNED_TEAM``. Replace the CSV when the organisers'
ownership table is available, keeping the three columns.

``uv run python -m backend.db.ownership`` (re)creates the ``team_ownership``
table; it touches no log table.
"""

from __future__ import annotations

import csv
from pathlib import Path

from psycopg2 import sql

from backend.db.config import OWNERSHIP_CSV_PATH, OWNERSHIP_TABLE

HEADER = ["dataset", "component", "team"]


def read_ownership_csv(path: Path | str = OWNERSHIP_CSV_PATH) -> list[tuple[str, str, str]]:
    """Read ``(dataset, component, team)`` rows, rejecting malformed input.

    Raises ``ValueError`` on a wrong header, an empty field, or a component
    mapped twice within one dataset, so a bad edit fails at load time rather
    than silently mis-attributing counts.
    """
    with Path(path).open(encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames != HEADER:
            raise ValueError(
                f"{path}: header must be dataset,component,team; got {reader.fieldnames}"
            )
        rows: list[tuple[str, str, str]] = []
        seen: set[tuple[str, str]] = set()
        for line, row in enumerate(reader, start=2):
            values = tuple((row.get(k) or "").strip() for k in HEADER)
            if not all(values):
                raise ValueError(f"{path}:{line}: empty field in {row}")
            key = (values[0], values[1])
            if key in seen:
                raise ValueError(f"{path}:{line}: {key} is mapped twice")
            seen.add(key)
            rows.append(values)  # type: ignore[arg-type]
    return rows


def load_ownership(path: Path | str = OWNERSHIP_CSV_PATH) -> int:
    """(Re)create the ``team_ownership`` table from the ownership CSV.

    The table is tiny, so it is inserted row by row from the host.
    """
    # Imported lazily so reading the CSV (prompts, tests) needs no DB driver setup.
    from backend.db.connection import db_cursor

    rows = read_ownership_csv(path)
    with db_cursor() as cur:
        cur.execute(
            sql.SQL("DROP TABLE IF EXISTS {t}").format(t=sql.Identifier(OWNERSHIP_TABLE))
        )
        cur.execute(
            sql.SQL(
                "CREATE TABLE {t} (dataset TEXT NOT NULL, component TEXT NOT NULL, "
                "team TEXT NOT NULL, PRIMARY KEY (dataset, component))"
            ).format(t=sql.Identifier(OWNERSHIP_TABLE))
        )
        insert = sql.SQL(
            "INSERT INTO {t} (dataset, component, team) VALUES (%s, %s, %s)"
        ).format(t=sql.Identifier(OWNERSHIP_TABLE))
        for row in rows:
            cur.execute(insert, row)
    print(f"Loaded {len(rows)} ownership rows into '{OWNERSHIP_TABLE}'.")
    return len(rows)


if __name__ == "__main__":
    load_ownership()
