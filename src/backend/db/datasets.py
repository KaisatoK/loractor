"""The dataset registry: one declarative description per log corpus.

Every query in :mod:`backend.db` is built against **canonical field names**, and
this module maps those names onto each corpus' real columns. Adding a corpus is
a registration here, not a new query builder -- that is the property
``tests/test_datasets.py`` asserts by querying BGL with no builder changes.

Canonical fields
----------------

==============  ==========================================================
``ts``          the row's timestamp; the mandatory filter on every query
``severity``    severity/level text
``service``     emitting service/component
``message``     the searchable log text
``entity_id``   the per-corpus correlation handle (pid, trace id, node)
``template``    the parsed event template, when the corpus has one
``event_id``    the template's short identifier, when the corpus has one
``record_id``   stable per-row primary key, when the corpus has one
==============  ==========================================================

A field a corpus lacks is simply absent from :attr:`Dataset.fields`, and the
capability set says so explicitly. ``record_id`` is the load-bearing example:
the OTel corpus has no primary key, so ``get_log_by_line_id_tool`` is gated on
:data:`Capability.RECORD_ID` rather than assuming one exists.

Why identifiers never come from the model
-----------------------------------------

Tools take a ``dataset`` id drawn from a closed vocabulary and resolve it here.
The model never supplies a table, index, or column name, so there is no path
from generated text into a SQL identifier.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Mapping

from psycopg2 import sql

from backend.db.config import EMBEDDING_DIMENSION, TABLE_NAME

# --- Canonical field names ---------------------------------------------------
TS = "ts"
SEVERITY = "severity"
SERVICE = "service"
MESSAGE = "message"
ENTITY_ID = "entity_id"
TEMPLATE = "template"
EVENT_ID = "event_id"
RECORD_ID = "record_id"

CANONICAL_FIELDS: tuple[str, ...] = (
    TS,
    SEVERITY,
    SERVICE,
    MESSAGE,
    ENTITY_ID,
    TEMPLATE,
    EVENT_ID,
    RECORD_ID,
)


class DatasetKind(StrEnum):
    """How the rows are reached."""

    NATIVE_TABLE = "native_table"
    """A real local table. Exact counts, cheap everything."""

    REMOTE_PARQUET = "remote_parquet"
    """Parquet over HTTPS, read in place via ``read_parquet``. No local copy."""

    LOCAL_FILE = "local_file"
    """A file loaded into a local table by a loader (loghub datasets)."""


# Fractional digits each timestamp precision carries. Python's ``datetime`` tops
# out at six, which is the line anything finer has to be truncated to before it
# can cross the driver boundary.
TS_PRECISIONS: dict[str, int] = {
    "second": 0,
    "millisecond": 3,
    "microsecond": 6,
    "nanosecond": 9,
}


class Capability(StrEnum):
    """What a dataset supports. Checked before a tool offers a feature."""

    BM25 = "bm25"
    """Has an inverted index with a text dictionary, so BM25 ranking works."""

    VECTOR = "vector"
    """Has embeddings, so semantic search works."""

    RECORD_ID = "record_id"
    """Has a stable per-row primary key, so fetch-by-id works."""

    EXACT_COUNTS = "exact_counts"
    """Aggregates are exact. Absent means results must be labelled approximate."""

    TEMPLATES = "templates"
    """Has parsed event templates, so template-level work is possible."""

    AUTO_COMPLETE = "auto_complete"
    """Has the verbatim + token index pair the type-ahead recipe needs."""

    CHUNK_SWEEP = "chunk_sweep"
    """Large enough, and addressable by row group, to support the sweep."""


@dataclass(frozen=True)
class Field:
    """One canonical field's binding to a corpus.

    ``column`` is a real column name, quoted as an identifier. ``expression`` is
    raw SQL for a derived field and is used verbatim -- it is **code-owned**, is
    never built from model output, and must stay that way.
    """

    column: str | None = None
    expression: str | None = None
    text_indexed: bool = False
    """Whether the inverted index carries this field verbatim (auto-complete)."""

    token_indexed: bool = False
    """Whether the token index carries this field split into words."""

    autocomplete_column: str | None = None
    """Column the type-ahead reads instead of ``column``, when they differ.

    Only text columns get a term dictionary in an inverted index, so an integer
    field needs a text mirror to be suggestable: OpenStack's ``pid`` is matched
    through ``pid_text``.
    """

    def __post_init__(self) -> None:
        if not self.column and not self.expression:
            raise ValueError("a Field needs either a column or an expression")

    def as_sql(self) -> sql.Composable:
        """The field as a SQL expression."""
        if self.expression:
            return sql.SQL(self.expression)
        return sql.Identifier(self.column)  # type: ignore[arg-type]

    def suggest_sql(self) -> sql.Composable:
        """The field as the type-ahead should read it."""
        if self.autocomplete_column:
            return sql.Identifier(self.autocomplete_column)
        return self.as_sql()

    def suggest_name(self) -> str | None:
        """Name of the column the type-ahead reads, or ``None`` if derived."""
        return self.autocomplete_column or self.column

    def as_alias(self) -> str:
        """A stable output-column name for result rows."""
        return self.column or "expr"


@dataclass(frozen=True)
class Dataset:
    """A declaratively described log corpus."""

    id: str
    kind: DatasetKind
    description: str
    sources: tuple[str, ...]
    """A table name for native/local kinds, or the HTTPS URL list for remote."""

    fields: Mapping[str, Field]
    capabilities: frozenset[Capability]
    severity_values: tuple[str, ...]
    """The corpus' own severity vocabulary, in the corpus' own casing.

    Measured, not assumed: OpenStack uses ``INFO``/``WARNING``/``ERROR`` and the
    OTel corpus uses lower-case ``info``/``error``/``warn``/``fatal``. Filtering
    with the wrong casing silently matches nothing.
    """

    ts_cast: str = "timestamp"
    """The cast applied to timestamp bounds (``timestamp`` or ``timestamptz``)."""

    ts_precision: str = "microsecond"
    """Precision of the timestamp column: a value from :data:`TS_PRECISIONS`.

    Declared per corpus rather than assumed, and **verifiable** against the
    source -- :func:`backend.db.probe.verify_ts_precision` reads the Parquet
    logical type and reports a mismatch instead of letting it surface as a
    driver error hundreds of seconds into a query.

    It matters because anything finer than ``microsecond`` cannot be handed to
    the Python driver: a value like ``2025-09-23 00:00:01.116886762+00`` makes
    psycopg2 read 116,886,762 as a microsecond field and raise *microsecond
    must be in 0..999999*. Comparisons and aggregates are unaffected, which is
    why it went unnoticed until a query first SELECTed a raw remote timestamp,
    so the truncation belongs on the way out rather than on the column.

    Measured on the OTel corpus: parts 0, 1, 7, 25 and 49 all report
    ``TimestampType(isAdjustedToUTC=1, unit=NANOS)`` with an identical
    26-entry schema, so one declaration covers the series.
    """

    index_name: str | None = None
    token_index_name: str | None = None
    selectors: Mapping[str, str] = field(default_factory=dict)
    """Auto-complete selector character -> canonical field name."""

    embedding_dimension: int = EMBEDDING_DIMENSION
    default_window_seconds: int = 3600
    max_rows: int = 1_000
    """Hard ceiling on rows any single query may return for this dataset."""

    row_estimate: int | None = None
    """Approximate corpus size, for cost estimates and refusal messages."""

    # --- capability helpers ------------------------------------------------
    def supports(self, capability: Capability) -> bool:
        return capability in self.capabilities

    @property
    def exact_counts(self) -> bool:
        return self.supports(Capability.EXACT_COUNTS)

    # --- field helpers -----------------------------------------------------
    def has_field(self, name: str) -> bool:
        return name in self.fields

    def field(self, name: str) -> Field:
        """Resolve a canonical field, or explain precisely why it is missing."""
        try:
            return self.fields[name]
        except KeyError:
            available = ", ".join(sorted(self.fields)) or "none"
            raise UnsupportedFieldError(
                f"Dataset '{self.id}' has no '{name}' field. Available: {available}."
            ) from None

    @property
    def ts_needs_truncation(self) -> bool:
        """Whether returning this timestamp to the client needs truncating.

        True for any precision finer than what Python's ``datetime`` can hold.
        """
        return TS_PRECISIONS.get(self.ts_precision, 6) > 6

    def sql_for(self, name: str) -> sql.Composable:
        """A canonical field as SQL, for use in SELECT/WHERE/GROUP BY."""
        return self.field(name).as_sql()

    def select_sql_for(self, name: str) -> sql.Composable:
        """A canonical field as SQL, safe to return to the client.

        Identical to :meth:`sql_for` except for a nanosecond timestamp, which is
        truncated to microseconds so the driver can parse it. Use this when a
        value is being SELECTed into result rows; use ``sql_for`` for
        comparisons and grouping, where full precision is both harmless and
        wanted.
        """
        if name == "ts" and self.ts_needs_truncation:
            return sql.SQL("date_trunc('microsecond', {col})").format(
                col=self.sql_for(name)
            )
        return self.sql_for(name)

    def column_for(self, name: str) -> str:
        """The output alias a canonical field lands under in result rows."""
        return self.field(name).as_alias()

    # --- relation helpers --------------------------------------------------
    def scan_relation(self) -> sql.Composable:
        """The FROM target that reads rows at source.

        A table for native/local corpora; a ``read_parquet`` over the URL list
        for remote ones. The list -- not a glob -- is the widening mechanism:
        HTTPS gives no bucket listing, and ``?`` wildcards are unsupported over
        S3, so a wider window means more entries here.
        """
        if self.kind is DatasetKind.REMOTE_PARQUET:
            # A single source is passed as a scalar path, not a one-element
            # list, and the difference is not cosmetic: a *list* argument makes
            # a view over the reader "generic", which disables the fast path an
            # external inverted index needs to materialize columns. With a list
            # the index can still count and match, but any query selecting a
            # column fails with "materialising real columns from this
            # view-backed inverted index is not yet supported".
            if len(self.sources) == 1:
                return sql.SQL("read_parquet({url})").format(
                    url=sql.Literal(self.sources[0])
                )
            urls = sql.SQL(", ").join(sql.Literal(u) for u in self.sources)
            return sql.SQL("read_parquet([{urls}])").format(urls=urls)
        return sql.Identifier(self.sources[0])

    def search_relation(self) -> sql.Composable:
        """The FROM target for full-text search.

        Queries run against the **index relation**, not the base table or view:
        that is what lets postings and ``INCLUDE`` columns answer without
        touching the source files.
        """
        if not self.index_name:
            raise UnsupportedFieldError(
                f"Dataset '{self.id}' has no inverted index to search."
            )
        return sql.Identifier(self.index_name)

    def token_relation(self) -> sql.Composable:
        if not self.token_index_name:
            raise UnsupportedFieldError(
                f"Dataset '{self.id}' has no token index."
            )
        return sql.Identifier(self.token_index_name)

    def selector_field(self, char: str) -> str | None:
        """Canonical field a type-ahead selector scopes to, if any.

        Returns ``None`` for a selector this corpus does not support, so the
        type-ahead degrades to "no suggestions" instead of erroring.
        """
        name = self.selectors.get(char)
        if name is None or name not in self.fields:
            return None
        return name


class UnknownDatasetError(KeyError):
    """Raised when a dataset id is not registered."""


class UnsupportedFieldError(KeyError):
    """Raised when a canonical field or relation is absent from a dataset."""


# --- Registrations -----------------------------------------------------------

# The original OpenStack demo corpus. This registration must reproduce the
# pre-registry behaviour exactly -- same table, same index names, same columns --
# and ``tests/test_datasets.py`` asserts it against the hardcoded constants.
OPENSTACK = Dataset(
    id="openstack",
    kind=DatasetKind.NATIVE_TABLE,
    description=(
        "OpenStack sample log (2k rows) with per-row embeddings. Small, exact, "
        "fully indexed; the default for demos and regression tests."
    ),
    sources=(TABLE_NAME,),
    fields=MappingProxyType(
        {
            TS: Field(column="log_timestamp"),
            SEVERITY: Field(column="category"),
            SERVICE: Field(column="service", text_indexed=True, token_indexed=True),
            MESSAGE: Field(column="content", text_indexed=True),
            ENTITY_ID: Field(
                column="pid", autocomplete_column="pid_text", token_indexed=True
            ),
            TEMPLATE: Field(
                column="event_template", text_indexed=True, token_indexed=True
            ),
            EVENT_ID: Field(column="event_id", text_indexed=True, token_indexed=True),
            RECORD_ID: Field(column="line_id"),
        }
    ),
    capabilities=frozenset(
        {
            Capability.BM25,
            Capability.VECTOR,
            Capability.RECORD_ID,
            Capability.EXACT_COUNTS,
            Capability.TEMPLATES,
            Capability.AUTO_COMPLETE,
        }
    ),
    severity_values=("INFO", "WARNING", "ERROR"),
    ts_cast="timestamp",
    index_name=f"{TABLE_NAME}_idx",
    token_index_name=f"{TABLE_NAME}_token_idx",
    selectors=MappingProxyType(
        {
            "@": SERVICE,
            "#": EVENT_ID,
            "^": TEMPLATE,
            "!": ENTITY_ID,
            "$": MESSAGE,
        }
    ),
    default_window_seconds=86_400,
    max_rows=1_000,
    row_estimate=2_000,
)


_REGISTRY: dict[str, Dataset] = {OPENSTACK.id: OPENSTACK}

DEFAULT_DATASET_ID = OPENSTACK.id


# --- The OTel corpus in public object storage --------------------------------
OTEL_BASE_URL = os.getenv(
    "OTEL_BASE_URL", "https://public-pme.s3.eu-west-3.amazonaws.com/text_bench"
)

# Which parts of the 50-file corpus are in scope. **This is the widening
# mechanism**: `OTEL_PARTS=0-9` covers the first five hours, `0-49` the full day.
# It is an explicit list rather than a glob because HTTPS gives no bucket
# listing and `?` wildcards are unsupported over S3.
OTEL_PARTS = os.getenv("OTEL_PARTS", "0")

# Derived template expression for the OTel corpus.
#
# Raw OTel has no parsed template, so one is derived by collapsing the variable
# parts of `Body`: long hex runs (trace/span/uuid-ish) and digit runs become
# `<*>`. This is **code-owned SQL**, never built from model output, and it is
# what makes template-level grouping and embedding possible on a corpus that
# ships no template column.
OTEL_TEMPLATE_EXPRESSION = (
    "regexp_replace(\"Body\", '[0-9a-fA-F]{8,}|[0-9]+', '<*>', 'g')"
)

# Severity vocabulary, in the corpus' own casing. MEASURED, not assumed: the
# corpus is lower case, so filtering on 'ERROR' silently matches nothing.
# Ordered by measured frequency.
OTEL_SEVERITY_VALUES = ("info", "error", "information", "trace", "warn", "fatal")

# Template embeddings for the OTel slice are opt-in. The requirement is to embed
# only when necessary; BGL needs semantic search, the OTel slice only gets it
# when asked for. The capability set reflects the configuration, so a disabled
# slice reports no VECTOR capability rather than failing at query time.
OTEL_SLICE_EMBEDDINGS = os.getenv("OTEL_SLICE_EMBEDDINGS", "false").lower() == "true"

OTEL_SLICE_TABLE = "otel_slice"


def _parse_parts(spec: str) -> tuple[int, ...]:
    """Parse an ``OTEL_PARTS`` spec into part numbers.

    Accepts comma-separated indices and inclusive ranges: ``0``, ``0,3,7``,
    ``0-9``, ``0-4,10``. Out-of-range parts are rejected loudly -- a typo that
    silently widened or narrowed the corpus would corrupt every cost estimate.
    """
    parts: list[int] = []
    for token in spec.split(","):
        token = token.strip()
        if not token:
            continue
        if "-" in token:
            lo_text, _, hi_text = token.partition("-")
            lo, hi = int(lo_text), int(hi_text)
            if lo > hi:
                raise ValueError(f"OTEL_PARTS range '{token}' is inverted")
            parts.extend(range(lo, hi + 1))
        else:
            parts.append(int(token))

    if not parts:
        raise ValueError(f"OTEL_PARTS '{spec}' selects no files")
    for part in parts:
        if not 0 <= part <= 49:
            raise ValueError(f"OTEL_PARTS part {part} is outside part_000..part_049")

    return tuple(sorted(dict.fromkeys(parts)))


def otel_part_urls(spec: str | None = None) -> tuple[str, ...]:
    """HTTPS URLs for the selected parts of the OTel corpus."""
    return tuple(
        f"{OTEL_BASE_URL}/part_{part:03d}.parquet"
        for part in _parse_parts(spec or OTEL_PARTS)
    )


_OTEL_URLS = otel_part_urls()

# Tier B -- the remote wide corpus, read in place.
#
# No local disk, no index, no primary key, and aggregates are approximate. Every
# one of those absences is declared rather than discovered at runtime: the
# missing EXACT_COUNTS capability is what forces results to be labelled
# approximate, and the missing RECORD_ID is what gates fetch-by-id off.
OTEL_S3 = (
    Dataset(
        id="otel_s3",
        kind=DatasetKind.REMOTE_PARQUET,
        description=(
            "OpenTelemetry demo-app logs as Parquet in public S3, queried in "
            f"place over {len(_OTEL_URLS)} of 50 files "
            f"({len(_OTEL_URLS)} billion rows). Timestamp-sorted. Aggregates "
            "are approximate and every query must carry a narrow time window."
        ),
        sources=_OTEL_URLS,
        fields=MappingProxyType(
            {
                TS: Field(column="Timestamp"),
                SEVERITY: Field(column="SeverityText"),
                SERVICE: Field(column="ServiceName"),
                MESSAGE: Field(column="Body"),
                ENTITY_ID: Field(column="TraceId"),
                TEMPLATE: Field(expression=OTEL_TEMPLATE_EXPRESSION),
            }
        ),
        capabilities=frozenset({Capability.TEMPLATES, Capability.CHUNK_SWEEP}),
        severity_values=OTEL_SEVERITY_VALUES,
        ts_cast="timestamptz",
        # Verified against parts 0, 1, 7, 25 and 49: TIMESTAMPTZ_NS throughout.
        ts_precision="nanosecond",
        
        index_name=None,
        token_index_name=None,
        selectors=MappingProxyType({}),
        default_window_seconds=60,
        max_rows=200,
        row_estimate=1_000_000_000 * len(_OTEL_URLS),
    )
)

# Tier A -- the standing searchable slice of the OTel corpus.
#
# A local table, severity-filtered AND time-bounded (measured: `error` alone is
# 13.8% of the corpus, so severity alone leaves ~138M rows -- far past budget).
# Materializing it adds the `line_id` the raw corpus lacks, which is what keeps
# fetch-by-id and the UI's "Line ID: n" affordance working on OTel data.
OTEL_SLICE = (
    Dataset(
        id="otel_slice",
        kind=DatasetKind.NATIVE_TABLE,
        description=(
            "Searchable slice of the OTel corpus: problem-severity rows from a "
            "bounded time window, materialized locally with a BM25 index. Small "
            "and exact, and the right target for text search."
        ),
        sources=(OTEL_SLICE_TABLE,),
        fields=MappingProxyType(
            {
                TS: Field(column="Timestamp"),
                SEVERITY: Field(column="SeverityText", text_indexed=True),
                SERVICE: Field(
                    column="ServiceName", text_indexed=True, token_indexed=True
                ),
                MESSAGE: Field(column="Body", text_indexed=True),
                ENTITY_ID: Field(column="TraceId"),
                TEMPLATE: Field(
                    column="EventTemplate", text_indexed=True, token_indexed=True
                ),
                RECORD_ID: Field(column="line_id"),
            }
        ),
        capabilities=frozenset(
            {
                Capability.BM25,
                Capability.RECORD_ID,
                Capability.EXACT_COUNTS,
                Capability.TEMPLATES,
                Capability.AUTO_COMPLETE,
            }
            | ({Capability.VECTOR} if OTEL_SLICE_EMBEDDINGS else set())
        ),
        severity_values=OTEL_SEVERITY_VALUES,
        ts_cast="timestamptz",
        index_name=f"{OTEL_SLICE_TABLE}_idx",
        token_index_name=f"{OTEL_SLICE_TABLE}_token_idx",
        selectors=MappingProxyType(
            {
                "@": SERVICE,
                "^": TEMPLATE,
                "$": MESSAGE,
            }
        ),
        default_window_seconds=1_800,
        max_rows=1_000,
        row_estimate=None,
    )
)


def register(dataset: Dataset) -> Dataset:
    """Add a dataset to the registry, rejecting duplicate ids."""
    if dataset.id in _REGISTRY:
        raise ValueError(f"dataset '{dataset.id}' is already registered")
    _REGISTRY[dataset.id] = dataset
    return dataset


def resolve(dataset_id: str | None) -> Dataset:
    """Resolve a dataset id to its registration.

    ``None`` resolves to the default corpus so existing call sites keep working.
    An unknown id raises rather than falling back, because a silent fallback
    would run a query against the wrong corpus.
    """
    if dataset_id is None:
        return _REGISTRY[DEFAULT_DATASET_ID]
    try:
        return _REGISTRY[dataset_id]
    except KeyError:
        known = ", ".join(sorted(_REGISTRY))
        raise UnknownDatasetError(
            f"Unknown dataset '{dataset_id}'. Registered: {known}."
        ) from None


def dataset_ids() -> tuple[str, ...]:
    """Registered ids, sorted, for closed-vocabulary tool schemas."""
    return tuple(sorted(_REGISTRY))


def all_datasets() -> tuple[Dataset, ...]:
    return tuple(_REGISTRY[k] for k in sorted(_REGISTRY))


def datasets_with(capability: Capability) -> tuple[Dataset, ...]:
    return tuple(d for d in all_datasets() if d.supports(capability))


# The OTel registrations are applied here, once ``register`` exists. The Dataset
# objects themselves are built above, next to the configuration that shapes them.
# --- BGL: Blue Gene/L supercomputer RAS logs (loghub) ------------------------
#
# The third corpus, and the one that proves the registry is an abstraction
# rather than a pair of special cases. It disagrees with both others:
# severities are UPPERCASE where OTel's are lowercase, its timestamp arrives as
# Unix epoch seconds, and its `Node` is high-cardinality enough to behave like a
# trace id rather than a service. None of that required new query code.
BGL_TABLE = "bgl"

BGL = Dataset(
    id="bgl",
    kind=DatasetKind.NATIVE_TABLE,
    description=(
        "Blue Gene/L supercomputer RAS log (loghub, 2k rows). Local, exact, "
        "fully indexed, with template-level embeddings. Spans seven months of "
        "2005-2006, so it is the corpus for long-range questions."
    ),
    sources=(BGL_TABLE,),
    fields=MappingProxyType(
        {
            TS: Field(column="Timestamp"),
            SEVERITY: Field(column="Level", text_indexed=True),
            SERVICE: Field(
                column="Component", text_indexed=True, token_indexed=True
            ),
            MESSAGE: Field(column="Content", text_indexed=True),
            ENTITY_ID: Field(column="Node", text_indexed=True, token_indexed=True),
            TEMPLATE: Field(
                column="EventTemplate", text_indexed=True, token_indexed=True
            ),
            # No EVENT_ID. loghub publishes parsed EventId/EventTemplate only
            # for its 2,000-line samples; the full 4.7M-line log carries
            # neither, so templates are derived at load and there is no event
            # id to map. Synthesising one would produce a hash no user would
            # ever type, while the template selector already answers "this kind
            # of event". otel_s3 omits record_id for the same reason -- a
            # canonical field is mapped only where the corpus really has it.
            RECORD_ID: Field(column="LineId"),
        }
    ),
    capabilities=frozenset(
        {
            Capability.BM25,
            Capability.RECORD_ID,
            Capability.EXACT_COUNTS,
            Capability.TEMPLATES,
            Capability.AUTO_COMPLETE,
        }
    ),
    # UPPERCASE, unlike OTel. A global severity constant would have been wrong
    # for one corpus or the other; the refusal names the right correction per
    # dataset because the vocabulary belongs to the dataset.
    # Six levels, confirmed against the full 4.7M-row corpus. FAILURE was
    # missing from the 2,000-line sample and would have been refused as unknown
    # on 1,714 real rows.
    severity_values=("INFO", "WARNING", "ERROR", "SEVERE", "FATAL", "FAILURE"),
    ts_cast="timestamptz",
    index_name=f"{BGL_TABLE}_idx",
    token_index_name=f"{BGL_TABLE}_token_idx",
    # Four selectors, not five: no event id to scope to.
    selectors=MappingProxyType(
        {
            "@": SERVICE,
            "^": TEMPLATE,
            "!": ENTITY_ID,
            "$": MESSAGE,
        }
    ),
    default_window_seconds=86_400,
    max_rows=1_000,
    row_estimate=4_747_963,
)

register(BGL)

register(OTEL_S3)
register(OTEL_SLICE)


def catalog() -> dict[str, dict]:
    """A model-facing summary of every corpus.

    Fed to the prompt layer so the agent knows which corpora exist, how big they
    are, and what each supports -- without ever seeing a table name.
    """
    return {
        d.id: {
            "description": d.description,
            "kind": str(d.kind),
            "approximate_rows": d.row_estimate,
            "severity_values": list(d.severity_values),
            "fields": sorted(d.fields),
            "capabilities": sorted(str(c) for c in d.capabilities),
            "exact_counts": d.exact_counts,
        }
        for d in all_datasets()
    }
