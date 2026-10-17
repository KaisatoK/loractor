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
import os
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from psycopg2 import sql

from backend.db.budget import estimate_index_bytes, fits
from backend.db.datasets import Dataset, resolve
from backend.db.errors import refuse

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

# Rows per COPY buffer flush. Keeps client memory flat for a corpus of any size.
# Rows per COPY buffer flush. Bounds client memory independently of corpus
# size: 4.7M rows stream through in ~95 flushes of this size.
COPY_CHUNK_ROWS = 50_000

# The COPY dialect is stated explicitly rather than left to the server's CSV
# sniffer.
#
# Sniffing a streamed body goes wrong in two ways that both corrupt a load:
# it reported ``quote = (empty)`` and then split a quoted message containing a
# comma across extra columns, and it guessed DATE for a VARCHAR column from a
# sample. Naming the dialect removes the guesswork; ``lineterminator`` on the
# writer side matches, since csv.writer emits CRLF by default.
_COPY_OPTIONS = sql.SQL(
    "WITH (FORMAT CSV, QUOTE '\"', ESCAPE '\"', NULL '')"
)


def _copy_writer(buffer: io.StringIO) -> "csv._writer":
    return csv.writer(buffer, lineterminator="\n", quoting=csv.QUOTE_MINIMAL)


class SourceKind(StrEnum):
    """Which of loghub's two distributions a corpus is loaded from."""

    STRUCTURED_CSV = "structured_csv"
    """A ``*_structured.csv`` with parsed ``EventId``/``EventTemplate``.

    Published only for the 2,000-line samples, so this is the fast path for
    development rather than the one that handles a real corpus.
    """

    RAW_LINES = "raw_lines"
    """The full ``*.log``: fixed space-separated fields then free-text content.

    Carries no parsed template, so templates are derived here. That is not a
    downgrade -- the OTel corpus works the same way, and deriving means the
    loader does not depend on someone having run a log parser first.
    """


@dataclass(frozen=True)
class LoghubSpec:
    """How to load one loghub corpus.

    ``columns`` is the physical table schema. ``derived`` holds SQL expressions
    computed on insert, used by the CSV path where values arrive verbatim;
    the raw path computes the same values client-side while it is already
    parsing each line, which avoids staging the corpus twice.
    """

    dataset_id: str
    csv_name: str
    columns: tuple[tuple[str, str], ...]
    derived: tuple[tuple[str, str], ...] = ()
    text_columns: tuple[str, ...] = ()
    verbatim_columns: tuple[str, ...] = ()
    token_columns: tuple[tuple[str, str], ...] = ()
    numeric_mirrors: tuple[tuple[str, str], ...] = field(default=())
    source_kind: SourceKind = SourceKind.STRUCTURED_CSV
    raw_name: str | None = None
    raw_fields: tuple[str, ...] = ()
    raw_content_column: str = "Content"
    anchor_column: str = ""
    anchor_values: frozenset[str] = frozenset()
    anchor_guard_column: str = ""
    anchor_guard_values: frozenset[str] = frozenset()
    path_env: str | None = None
    expected_rows: int | None = None

    @property
    def csv_path(self) -> Path:
        return DATA_DIR / self.csv_name

    @property
    def source_path(self) -> Path:
        """Where the corpus actually lives.

        A full corpus is hundreds of megabytes, so it is deliberately *not*
        expected inside the repository: the environment variable wins, letting
        large data sit on whatever volume has room.
        """
        if self.path_env:
            override = os.getenv(self.path_env)
            if override:
                return Path(override).expanduser()
        if self.source_kind is SourceKind.RAW_LINES and self.raw_name:
            return DATA_DIR / self.raw_name
        return self.csv_path

    @property
    def all_column_names(self) -> tuple[str, ...]:
        return tuple(name for name, _ in self.columns) + tuple(
            name for name, _ in self.derived
        )


# Variable parts of a message, collapsed so that lines differing only in ids,
# addresses or counts share one template.
#
# Deliberately the same idea as the OTel corpus's SQL-side expression, in the
# same order (long hex before plain digits, so an address is not shredded into
# numbers). Applied client-side here because the raw loader is already parsing
# every line, and running it in SQL would mean staging 743 MB of corpus twice.
_TEMPLATE_PATTERNS = (
    re.compile(r"0x[0-9a-fA-F]+"),
    re.compile(r"\b[0-9a-fA-F]{8,}\b"),
    re.compile(r"\b\d+\b"),
)

# Node/core identifiers such as R02-M1-N0-C:J12-U11 appear inside message text
# as well as in their own column; collapsing them keeps one template per event
# kind instead of one per node.
_NODE_PATTERN = re.compile(r"\bR\d+-M\d+-N\d+(-[CI]:J\d+-U\d+)?\b")


def derive_template(content: str) -> str:
    """Collapse a message to its event template.

    The raw corpus ships no parsed template, so this is what makes template
    embeddings -- and therefore semantic search -- possible on the full dataset.
    """
    text = _NODE_PATTERN.sub("<*>", content)
    for pattern in _TEMPLATE_PATTERNS:
        text = pattern.sub("<*>", text)
    return text


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

# The full corpus: 4,747,963 lines, 743 MB of raw text.
#
# This is the registered loader for ``bgl``; the 2,000-line structured sample
# above remains available as ``bgl_2k`` for fast iteration. Templates are
# derived rather than read, because loghub publishes parsed templates only for
# the samples.
BGL_FULL_SPEC = LoghubSpec(
    dataset_id="bgl",
    csv_name="BGL_2k.log_structured.csv",
    raw_name="BGL.log",
    path_env="BGL_LOG_PATH",
    source_kind=SourceKind.RAW_LINES,
    expected_rows=4_747_963,
    # Fields are located by vocabulary, not by position: BGL's arity varies.
    anchor_column="Level",
    anchor_values=frozenset(
        {"INFO", "WARNING", "ERROR", "SEVERE", "FATAL", "FAILURE"}
    ),
    anchor_guard_column="Type",
    anchor_guard_values=frozenset({"RAS", "NULL"}),
    # Nine fixed whitespace-separated fields, then the message. ``Content`` is
    # everything after the ninth, so a message containing spaces stays intact.
    raw_fields=(
        "Label",
        "EpochSeconds",
        "Date",
        "Node",
        "RawTime",
        "NodeRepeat",
        "Type",
        "Component",
        "Level",
    ),
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
        ("EventTemplate", "VARCHAR"),
        ("Timestamp", "TIMESTAMPTZ"),
    ),
    text_columns=("Content",),
    verbatim_columns=("Component", "Level", "EventTemplate", "Node", "Label"),
    token_columns=(
        ("Component", "dotted"),
        ("EventTemplate", "english"),
        ("Node", "english"),
    ),
)

SPECS: dict[str, LoghubSpec] = {
    "bgl": BGL_FULL_SPEC,
    "bgl_2k": BGL_SPEC,
}

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
        writer = _copy_writer(buffer)
        pending = 0

        def flush() -> None:
            nonlocal pending
            if not pending:
                return
            buffer.seek(0)
            cur.copy_expert(
                sql.SQL("COPY {t} ({cols}) FROM STDIN {options}").format(
                    t=sql.Identifier(f"{spec.dataset_id}_staging"),
                    cols=sql.SQL(", ").join(sql.Identifier(c) for c in physical),
                    options=_COPY_OPTIONS,
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


def parse_raw_line(spec: LoghubSpec, line: str) -> dict | None:
    """Split one raw log line into fields, anchored on a known vocabulary.

    Positional parsing is not safe on this corpus, and the failure is silent
    rather than loud. BGL omits ``NodeRepeat`` on 306 lines, which shifts every
    later field by one: a fixed offset then reads ``KERNEL`` as the type,
    ``FATAL`` as the component and the first word of the message as the
    severity. Nothing errors -- the rows simply land mislabelled, and a severity
    query quietly misses them.

    So the fields are located rather than counted. ``Level`` is found by its own
    small vocabulary, guarded by ``Type`` two positions earlier, whose
    vocabulary is only ``RAS`` and ``NULL``. Requiring both makes a false match
    on message text essentially impossible, and it anchors every one of the
    4,747,963 lines -- including ten that are corrupt in the source, where a
    fragment of an earlier message is spliced into the node field.
    """
    parts = line.split()
    if len(parts) < len(spec.raw_fields):
        return None

    level_index = next(
        (
            i
            for i in range(6, len(parts))
            if parts[i] in spec.anchor_values
            and parts[i - 2] in spec.anchor_guard_values
        ),
        None,
    )
    if level_index is None:
        return None

    # Fields before the anchor are addressed backwards from it; the fixed head
    # (label, epoch, date, node, time) is addressed forwards. NodeRepeat exists
    # only when there is room for it between the two.
    node_repeat_index = level_index - 3
    return {
        "Label": parts[0],
        "EpochSeconds": parts[1],
        "Date": parts[2],
        "Node": parts[3],
        "RawTime": parts[4],
        "NodeRepeat": parts[node_repeat_index] if node_repeat_index > 4 else None,
        "Type": parts[level_index - 2],
        "Component": parts[level_index - 1],
        "Level": parts[level_index],
        # An empty message is a real record: 34,470 BGL rows carry every field
        # and no message, most of them FATAL.
        "Content": " ".join(parts[level_index + 1 :]),
    }


def _stream_raw_lines(spec: LoghubSpec, cur, table: str, progress=None) -> int:
    """Parse the raw log and COPY it straight into the final table.

    Single pass, by design. The CSV path stages rows and then projects them,
    which doubles a corpus on disk -- acceptable for 2,000 rows, not for 743 MB.
    Here the line is already being split in Python, so the derived timestamp and
    template are computed in the same pass and COPY writes the finished row.

    Memory stays flat regardless of corpus size: lines are read lazily and
    flushed every :data:`COPY_CHUNK_ROWS`.
    """
    physical = [name for name, _ in spec.columns]
    field_count = len(spec.raw_fields)
    copied = 0
    skipped = 0

    copy_statement = sql.SQL("COPY {t} ({cols}) FROM STDIN {options}").format(
        t=sql.Identifier(table),
        cols=sql.SQL(", ").join(sql.Identifier(c) for c in physical),
        options=_COPY_OPTIONS,
    ).as_string(cur)

    buffer = io.StringIO()
    writer = _copy_writer(buffer)
    pending = 0

    def flush() -> None:
        nonlocal pending
        if not pending:
            return
        buffer.seek(0)
        cur.copy_expert(copy_statement, buffer)
        buffer.seek(0)
        buffer.truncate(0)
        pending = 0

    with spec.source_path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.rstrip("\n")
            if not line:
                continue
            values = parse_raw_line(spec, line)
            if values is None:
                # Counted rather than dropped silently, so the report can say
                # the corpus was not wholly loadable.
                skipped += 1
                continue

            try:
                epoch = int(values["EpochSeconds"])
            except ValueError:
                skipped += 1
                continue

            content = values["Content"]
            copied += 1
            row = {
                **values,
                "LineId": copied,
                "EventTemplate": derive_template(content),
                "Timestamp": datetime.fromtimestamp(epoch, UTC).isoformat(sep=" "),
            }
            writer.writerow([row.get(name) for name in physical])
            pending += 1

            if pending >= COPY_CHUNK_ROWS:
                flush()
                if progress is not None:
                    progress(copied, skipped)
        flush()

    if skipped:
        logger.warning("Skipped %d unparseable lines in %s", skipped, spec.source_path)
    return copied


def load(
    dataset: Dataset | str = "bgl", *, rebuild: bool = True, progress=None
) -> dict:
    """Load a loghub corpus into a local indexed table.

    Deliberately embeds nothing: semantic search comes from
    :func:`backend.db.templates.build_templates`, which embeds the corpus's
    distinct *templates* rather than its rows. That is what keeps a 4.7M-row
    corpus affordable -- thousands of embedding calls instead of millions.
    """
    spec = spec_for(dataset)
    ds = resolve(spec.dataset_id)
    table = spec.dataset_id
    staging = f"{table}_staging"
    raw = spec.source_kind is SourceKind.RAW_LINES

    if not spec.source_path.exists():
        hint = (
            f"Set {spec.path_env} to the file, or place {spec.raw_name} in "
            f"{DATA_DIR}."
            if spec.path_env
            else f"Place {spec.csv_name} in {DATA_DIR}."
        )
        raise refuse(
            f"{spec.source_path} is not present.",
            hint,
            code="missing_source",
            dataset=spec.dataset_id,
        )

    report: dict = {
        "dataset": spec.dataset_id,
        "table": table,
        "source": str(spec.source_path),
        "source_kind": spec.source_kind.value,
    }

    with _load_cursor() as cur:
        # A full corpus is large enough that the budget has to be checked
        # before loading rather than discovered afterwards.
        if spec.expected_rows:
            cur.execute("SELECT pg_database_size(current_database()) AS b")
            verdict = fits(
                estimate_index_bytes(spec.expected_rows, materialized=True),
                dataset_id=spec.dataset_id,
                current_db_bytes=int(cur.fetchone()["b"]),
            )
            if not verdict.ok:
                raise refuse(
                    verdict.reason,
                    verdict.suggestion,
                    code="over_budget",
                    dataset=spec.dataset_id,
                )
            report["budget"] = verdict.reason
        if rebuild:
            for name in (table, staging):
                cur.execute(
                    sql.SQL("DROP TABLE IF EXISTS {t} CASCADE").format(
                        t=sql.Identifier(name)
                    )
                )

        target = table if raw else staging
        cur.execute(
            sql.SQL("CREATE TABLE {t} ({cols})").format(
                t=sql.Identifier(target),
                cols=sql.SQL(", ").join(
                    sql.SQL("{name} {type}").format(
                        name=sql.Identifier(name), type=sql.SQL(coltype)
                    )
                    for name, coltype in spec.columns
                ),
            )
        )

        started = time.monotonic()
        if raw:
            report["rows_copied"] = _stream_raw_lines(
                spec, cur, table, progress=progress
            )
        else:
            report["rows_copied"] = _stream_rows(spec, cur)
        report["copy_seconds"] = round(time.monotonic() - started, 2)

        if not raw:
            # The CSV path projects staging into the final table to materialize
            # derived columns, because COPY cannot compute. The raw path has
            # already computed them while parsing, so it skips this entirely --
            # which is what keeps a 743 MB corpus from being staged twice.
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
            cur.execute(sql.SQL("DROP TABLE {t}").format(t=sql.Identifier(staging)))

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
