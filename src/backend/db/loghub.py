"""Loader for loghub's structured log corpora.

`loghub <https://github.com/logpai/loghub>`_ publishes many real log corpora in
one shape: a ``*_structured.csv`` with per-line fields plus a parsed
``EventId``/``EventTemplate`` pair. That regularity is the point of this module
-- adding another corpus should be a :class:`LoghubSpec` and a dataset
registration, not new query code.

The test that matters for this claim is in ``tests/test_bgl.py``: every query
builder must work on BGL with **no new SQL**. If adding a corpus required
touching ``count_query`` or ``query``, the registry would not actually be an
abstraction.

Rows are streamed with ``COPY ... FROM STDIN`` rather than read by the server
from a path. The existing OpenStack importer uses ``read_csv``, which executes
*inside* the server and therefore needs the file copied into the container with
docker. Streaming removes that dependency entirely, so a loader works against
any reachable SereneDB regardless of where it runs.
"""

from __future__ import annotations

import csv
import io
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from psycopg2 import sql

from backend.db.datasets import Dataset, resolve
from backend.db.errors import refuse

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

# Rows per COPY buffer flush. Keeps client memory flat for a corpus of any size.
COPY_CHUNK_ROWS = 5_000


@dataclass(frozen=True)
class LoghubSpec:
    """How to load one loghub corpus.

    ``columns`` is the physical table schema; ``derived`` holds expressions
    computed on insert, which is where a corpus's own timestamp convention is
    normalised to a real ``TIMESTAMPTZ``.
    """

    dataset_id: str
    csv_name: str
    columns: tuple[tuple[str, str], ...]
    derived: tuple[tuple[str, str], ...] = ()
    text_columns: tuple[str, ...] = ()
    verbatim_columns: tuple[str, ...] = ()
    token_columns: tuple[tuple[str, str], ...] = ()
    numeric_mirrors: tuple[tuple[str, str], ...] = field(default=())

    @property
    def csv_path(self) -> Path:
        return DATA_DIR / self.csv_name

    @property
    def all_column_names(self) -> tuple[str, ...]:
        return tuple(name for name, _ in self.columns) + tuple(
            name for name, _ in self.derived
        )


# BGL: Blue Gene/L supercomputer RAS logs.
#
# Chosen as the third corpus precisely because it is awkward in ways OpenStack
# and OTel are not:
#
# * severities are **UPPERCASE** (``INFO``, ``FATAL``, ``SEVERE``) where OTel's
#   are lowercase, so the per-dataset severity vocabulary has to be real rather
#   than a global constant;
# * the timestamp is Unix epoch **seconds** in a column named ``Timestamp``,
#   while a separate ``Time`` column holds a non-ISO string
#   (``2005-06-03-15.42.50.675872``) that no database will parse;
# * ``Node`` is high cardinality (1,778 distinct in 2,000 rows), so it behaves
#   like a trace id rather than a service;
# * it carries a ``Label`` column that is anomaly ground truth (``-`` for
#   normal), which no other corpus here has.
BGL_SPEC = LoghubSpec(
    dataset_id="bgl",
    csv_name="BGL_2k.log_structured.csv",
    columns=(
        ("LineId", "BIGINT"),
        ("Label", "VARCHAR"),
        ("EpochSeconds", "BIGINT"),
        ("Date", "VARCHAR"),
        ("Node", "VARCHAR"),
        ("RawTime", "VARCHAR"),
        ("NodeRepeat", "VARCHAR"),
        ("Type", "VARCHAR"),
        ("Component", "VARCHAR"),
        ("Level", "VARCHAR"),
        ("Content", "VARCHAR"),
        ("EventId", "VARCHAR"),
        ("EventTemplate", "VARCHAR"),
    ),
    # The CSV's own ``Time`` string is unparseable, but ``Timestamp`` is clean
    # epoch seconds, so the real timestamp is derived from that.
    # Identifiers in a derived expression must be quoted: SereneDB folds
    # unquoted names to lower case, and loghub's columns are CamelCase.
    derived=(("Timestamp", 'to_timestamp("EpochSeconds")'),),
    text_columns=("Content",),
    verbatim_columns=("Component", "Level", "EventId", "EventTemplate", "Node", "Label"),
    # Every selector-addressable column needs a word-split entry: the
    # auto-complete recipe matches through the token relation, so a column
    # missing here fails with "@@ requires an inverted-indexed column".
    token_columns=(
        ("Component", "dotted"),
        ("EventId", "english"),
        ("EventTemplate", "english"),
        ("Node", "english"),
    ),
)

SPECS: dict[str, LoghubSpec] = {BGL_SPEC.dataset_id: BGL_SPEC}

# The CSV header loghub ships, mapped onto physical column names. Only entries
# that differ are listed; anything else is taken verbatim.
_HEADER_RENAMES = {"Timestamp": "EpochSeconds", "Time": "RawTime"}


def spec_for(dataset: Dataset | str) -> LoghubSpec:
    """The loader spec for a dataset, or a refusal naming what is loadable."""
    dataset_id = dataset if isinstance(dataset, str) else dataset.id
    try:
        return SPECS[dataset_id]
    except KeyError:
        raise refuse(
            f"No loghub loader is registered for '{dataset_id}'.",
            "Loadable corpora: " + ", ".join(sorted(SPECS)),
            code="unknown_dataset",
            dataset=dataset_id,
        ) from None


def _load_cursor():
    from backend.db.probe import probe_cursor

    return probe_cursor()


def _stream_rows(spec: LoghubSpec, cur) -> int:
    """COPY the CSV into the staging table, in bounded chunks.

    Values are re-emitted through :mod:`csv` rather than passed through as raw
    lines, so a quoted field containing a comma or newline survives the round
    trip intact -- log messages contain both.
    """
    physical = [name for name, _ in spec.columns]
    copied = 0

    with spec.csv_path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        header = [_HEADER_RENAMES.get(name, name) for name in (reader.fieldnames or [])]
        missing = set(physical) - set(header)
        if missing:
            raise refuse(
                f"{spec.csv_name} is missing columns: {sorted(missing)}.",
                "Use the loghub *_structured.csv for this corpus.",
                code="bad_source",
                dataset=spec.dataset_id,
            )

        buffer = io.StringIO()
        writer = csv.writer(buffer)
        pending = 0

        def flush() -> None:
            nonlocal pending
            if not pending:
                return
            buffer.seek(0)
            cur.copy_expert(
                sql.SQL("COPY {t} ({cols}) FROM STDIN WITH CSV").format(
                    t=sql.Identifier(f"{spec.dataset_id}_staging"),
                    cols=sql.SQL(", ").join(sql.Identifier(c) for c in physical),
                ).as_string(cur),
                buffer,
            )
            buffer.seek(0)
            buffer.truncate(0)
            pending = 0

        for record in reader:
            row = {
                _HEADER_RENAMES.get(key, key): value for key, value in record.items()
            }
            writer.writerow([row.get(name) for name in physical])
            pending += 1
            copied += 1
            if pending >= COPY_CHUNK_ROWS:
                flush()
        flush()

    return copied


def load(dataset: Dataset | str = "bgl", *, rebuild: bool = True) -> dict:
    """Load a loghub corpus into a local indexed table.

    Deliberately does not embed anything: semantic search comes from
    :func:`backend.db.templates.build_templates`, which embeds the corpus's
    distinct templates. BGL has 120 templates for 2,000 rows, so that is 120
    embedding calls rather than 2,000.
    """
    spec = spec_for(dataset)
    ds = resolve(spec.dataset_id)
    table = spec.dataset_id
    staging = f"{table}_staging"

    if not spec.csv_path.exists():
        raise refuse(
            f"{spec.csv_name} is not present in {DATA_DIR}.",
            "Download the loghub structured CSV for this corpus into "
            f"{DATA_DIR}.",
            code="missing_source",
            dataset=spec.dataset_id,
        )

    report: dict = {"dataset": spec.dataset_id, "table": table}

    with _load_cursor() as cur:
        if rebuild:
            for name in (table, staging):
                cur.execute(
                    sql.SQL("DROP TABLE IF EXISTS {t} CASCADE").format(
                        t=sql.Identifier(name)
                    )
                )

        cur.execute(
            sql.SQL("CREATE TABLE {t} ({cols})").format(
                t=sql.Identifier(staging),
                cols=sql.SQL(", ").join(
                    sql.SQL("{name} {type}").format(
                        name=sql.Identifier(name), type=sql.SQL(coltype)
                    )
                    for name, coltype in spec.columns
                ),
            )
        )

        started = time.monotonic()
        report["rows_copied"] = _stream_rows(spec, cur)
        report["copy_seconds"] = round(time.monotonic() - started, 2)

        # Project the staging table into the final one, materializing derived
        # columns. Two steps rather than one because COPY cannot compute.
        select_list = [sql.Identifier(name) for name, _ in spec.columns]
        select_list += [
            sql.SQL("{expr} AS {name}").format(
                expr=sql.SQL(expression), name=sql.Identifier(name)
            )
            for name, expression in spec.derived
        ]
        cur.execute(
            sql.SQL("CREATE TABLE {t} AS SELECT {cols} FROM {staging}").format(
                t=sql.Identifier(table),
                cols=sql.SQL(", ").join(select_list),
                staging=sql.Identifier(staging),
            )
        )
        cur.execute(
            sql.SQL("DROP TABLE {t}").format(t=sql.Identifier(staging))
        )

        cur.execute(
            sql.SQL("SELECT count(*) AS n FROM {t}").format(t=sql.Identifier(table))
        )
        report["rows"] = int(cur.fetchone()["n"])

        _ensure_dictionaries(cur)

        started = time.monotonic()
        _build_indexes(cur, spec, ds)
        report["index_seconds"] = round(time.monotonic() - started, 2)

        cur.execute(
            sql.SQL("VACUUM (REFRESH_TABLE) {t}").format(t=sql.Identifier(table))
        )
        cur.execute(
            sql.SQL("SELECT pg_total_relation_size({t}) AS b").format(
                t=sql.Literal(table)
            )
        )
        report["bytes"] = int(cur.fetchone()["b"])

        cur.execute(
            sql.SQL("""
                SELECT min("Timestamp") AS lo, max("Timestamp") AS hi
                FROM {t}
            """).format(t=sql.Identifier(table))
        )
        bounds = cur.fetchone()
        report["covered_start"] = str(bounds["lo"])
        report["covered_end"] = str(bounds["hi"])

    logger.info("Loaded %s: %s", spec.dataset_id, report)
    return report


def _ensure_dictionaries(cur) -> None:
    """The shared text search dictionaries, so ranking matches other corpora."""
    cur.execute("""
        CREATE TEXT SEARCH DICTIONARY IF NOT EXISTS english AS
        split_text(case := 'lower') WITH (frequency, position, offset)
    """)
    cur.execute("""
        CREATE TEXT SEARCH DICTIONARY IF NOT EXISTS dotted AS
        split_by_non_alpha() | normalize_tokens('en_US.UTF-8', case := 'lower')
        WITH (frequency, position)
    """)


def _build_indexes(cur, spec: LoghubSpec, dataset: Dataset) -> None:
    """The verbatim + token index pair the whole query layer expects.

    Index names come from the dataset registration, not from this module, so the
    registry stays the single source of truth for what a query will read.
    """
    record_id = dataset.sql_for("record_id")
    entries: list[sql.Composable] = [record_id]
    entries += [
        sql.SQL("{col} english").format(col=sql.Identifier(col))
        for col in spec.text_columns
    ]
    entries += [sql.Identifier(col) for col in spec.verbatim_columns]

    cur.execute(
        sql.SQL("CREATE INDEX {idx} ON {t} USING inverted ({entries})").format(
            idx=sql.Identifier(dataset.index_name),
            t=sql.Identifier(spec.dataset_id),
            entries=sql.SQL(", ").join(entries),
        )
    )

    if spec.token_columns and dataset.token_index_name:
        cur.execute(
            sql.SQL("CREATE INDEX {idx} ON {t} USING inverted ({entries})").format(
                idx=sql.Identifier(dataset.token_index_name),
                t=sql.Identifier(spec.dataset_id),
                entries=sql.SQL(", ").join(
                    sql.SQL("{col} {dict}").format(
                        col=sql.Identifier(col), dict=sql.SQL(dictionary)
                    )
                    for col, dictionary in spec.token_columns
                ),
            )
        )


def status(dataset: Dataset | str = "bgl") -> dict:
    """Whether a loghub corpus is loaded, and what it covers."""
    spec = spec_for(dataset)
    with _load_cursor() as cur:
        cur.execute(
            "SELECT count(*) AS n FROM information_schema.tables "
            "WHERE table_name = %s",
            (spec.dataset_id,),
        )
        if not int(cur.fetchone()["n"]):
            return {"exists": False, "dataset": spec.dataset_id}

        cur.execute(
            sql.SQL("""
                SELECT count(*) AS rows,
                       min("Timestamp") AS lo,
                       max("Timestamp") AS hi
                FROM {t}
            """).format(t=sql.Identifier(spec.dataset_id))
        )
        row = cur.fetchone()

    return {
        "exists": True,
        "dataset": spec.dataset_id,
        "rows": int(row["rows"]),
        "covered_start": str(row["lo"]),
        "covered_end": str(row["hi"]),
    }


def main() -> None:
    """Load a loghub corpus and embed its templates.

    Separate from ``backend.db.setup`` on purpose: that module drops and
    recreates the OpenStack table, which is not something loading a different
    corpus should ever do.
    """
    import argparse
    import json

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", nargs="?", default="bgl", choices=sorted(SPECS))
    parser.add_argument(
        "--no-embed",
        action="store_true",
        help="load rows and indexes but skip template embeddings",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    print(json.dumps(load(args.dataset), indent=2))

    if not args.no_embed:
        from backend.db.templates import build_templates

        print(json.dumps(build_templates(args.dataset, rebuild=True), indent=2))


if __name__ == "__main__":
    main()
