"""Tool argument schemas.

Two rules shape every schema here:

**The model never names a SQL object.** The old schemas carried
``table_name: str``, so the relation a query ran against came straight from
generated text. It is replaced by ``dataset``, a closed vocabulary of registered
corpus ids resolved server-side through :mod:`backend.db.datasets`. There is now
no path from model output to a SQL identifier.

**Bounds are part of the contract.** A time window is mandatory wherever rows are
scanned, and ``limit`` carries a ceiling. On a billion-row corpus an unbounded
query is not slow, it is fatal, so the schema refuses to express one.

``dataset`` is a plain ``str`` validated against the registry rather than a
``Literal``, because the registry is the single source of truth and a ``Literal``
would have to be kept in sync with it by hand.
"""

from typing import Annotated, Literal, Self

from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic.json_schema import WithJsonSchema

from backend.db.datasets import (
    DEFAULT_DATASET_ID,
    Capability,
    dataset_ids,
    datasets_with,
    local_datasets,
)

# Accepts 'YYYY-MM-DD HH:MM:SS' plus optional fractional seconds and UTC offset,
# because the OTel corpus' timestamps are timestamptz and its own metadata
# reports them as '2025-09-23 00:00:01.116880211+00'.
DATETIME_REGEX = (
    r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(\.\d{1,9})?([+-]\d{2}(:?\d{2})?|Z)?$"
)

_DATASET_DESCRIPTION = (
    "Which log corpus to query. One of: " + ", ".join(dataset_ids()) + "."
)

CanonicalField = Literal["service", "entity_id", "event_id", "template", "severity"]
# Every bucket the query layer supports (``predicates.BUCKETS``). The schema
# used to stop at 'day', so a trend refused with "bucket by week instead" could
# not follow its own suggestion.
Bucket = Literal["second", "minute", "hour", "day", "week", "month"]

# Parameters shared by several tools. A constraint or a description written here
# is advertised to every client and enforced on every transport.
SeverityFilter = Annotated[
    str | None,
    Field(
        description=(
            "Exact severity to filter on, in the corpus' own casing (OpenStack "
            "and BGL use 'ERROR', the OTel corpus 'error'). Take the real values "
            "from list_datasets_tool. Omit for all severities."
        )
    ),
]


def _vocabulary(ids) -> WithJsonSchema:
    """A closed vocabulary, advertised as a JSON-schema ``enum``.

    Kept as annotation *metadata* rather than ``Field(json_schema_extra=...)``:
    langchain-core rebuilds each tool's schema from the field metadata and drops
    the extras, so only metadata reaches the model on the local transport.
    Validation stays with the registry-backed validators below, so a dataset
    registered later is still accepted.
    """
    return WithJsonSchema({"type": "string", "enum": list(ids)})


LocalDatasetId = Annotated[
    str, _vocabulary(d.id for d in local_datasets())
]

# Every registered dataset, remote included (``rank_priorities_tool`` accepts the
# OTel tiers as well as the local corpora).
DatasetId = Annotated[str, _vocabulary(dataset_ids())]


class _DatasetScoped(BaseModel):
    """Base for every tool argument set: a validated corpus id."""

    dataset: Annotated[
        str,
        _vocabulary(dataset_ids()),
        Field(description=_DATASET_DESCRIPTION),
    ] = DEFAULT_DATASET_ID

    @field_validator("dataset")
    @classmethod
    def _known_dataset(cls, value: str) -> str:
        if value not in dataset_ids():
            raise ValueError(
                f"Unknown dataset '{value}'. Choose one of: "
                f"{', '.join(dataset_ids())}."
            )
        return value


class _Windowed(_DatasetScoped):
    """Argument set that scans rows, and therefore needs a time window."""

    start_time: Annotated[
        str,
        Field(
            pattern=DATETIME_REGEX,
            description=(
                "Window start, 'YYYY-MM-DD HH:MM:SS'. Add a UTC offset "
                "('+00') for the OTel corpus. Required: the window is the cost "
                "control on a large corpus."
            ),
        ),
    ]
    end_time: Annotated[
        str,
        Field(
            pattern=DATETIME_REGEX,
            description="Window end, exclusive. Must be after start_time.",
        ),
    ]

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.end_time <= self.start_time:
            raise ValueError("end_time must be after start_time")
        return self


class CountByCategory(_Windowed):
    """Counts grouped by severity, optionally broken down by another field."""

    type: Annotated[
        Literal["general", "service", "entity_id", "template", "event_id"],
        Field(
            description=(
                "Breakdown dimension. 'general' counts by severity only; the "
                "others add that field. 'template' is the parsed event template "
                "(a recurring pattern); 'event_id' only where the corpus has one. "
                "'entity_id' is the correlation handle (process id on OpenStack, "
                "node on BGL, trace id on OTel)."
            )
        ),
    ] = "general"
    category: SeverityFilter = None
    limit: Annotated[
        int, Field(ge=1, le=200, description="Most rows to return (default 50).")
    ] = 50


class GetTrend(_Windowed):
    """Per-bucket counts for one known field value."""

    type: Annotated[
        tuple[CanonicalField, str],
        Field(
            description=(
                "(field, exact value) -- for example ('service', "
                "'cartservice'). Discover valid values with get_data_tool "
                "first."
            )
        ),
    ]
    bucket: Annotated[
        Bucket | None,
        Field(
            description=(
                "Bucket width. Omit it to let the window decide: the finest "
                "bucket that keeps the series to about 100 points. A window "
                "that would need more than 240 points is refused with a "
                "coarser bucket suggested."
            )
        ),
    ] = None
    category: SeverityFilter = None
    group_by: Annotated[
        Literal["severity"] | None,
        Field(
            description=(
                "'severity' returns one series per severity level that occurs "
                "in the window (rows gain a severity column), each with its own "
                "direction of travel. Omit for a single series."
            )
        ),
    ] = None


class SearchLogs(_DatasetScoped):
    """Full-text search over an indexed corpus, optionally within a time window.

    The window is optional, unlike every scanning tool's: search reads the
    inverted index, whose size is already bounded by whatever built it, so a
    window narrows the question rather than bounding the cost.
    """

    query_text: Annotated[
        str,
        Field(
            min_length=1,
            max_length=500,
            description=(
                "Text or concept to find in log message bodies. Do not put a "
                "severity here merely as a filter -- use category."
            ),
        ),
    ]
    category: SeverityFilter = None
    limit: Annotated[
        int,
        Field(
            ge=1,
            le=50,
            description=(
                "Most example rows to return (default 10). total_matches still "
                "counts every match."
            ),
        ),
    ] = 10
    start_time: Annotated[
        str | None,
        Field(
            pattern=DATETIME_REGEX,
            description=(
                "Optional window start, 'YYYY-MM-DD HH:MM:SS' (add '+00' on the "
                "OTel corpus). Give it together with end_time, or neither; "
                "the dataset's window limit applies."
            ),
        ),
    ] = None
    end_time: Annotated[
        str | None,
        Field(
            pattern=DATETIME_REGEX,
            description="Optional window end, exclusive. Required with start_time.",
        ),
    ] = None

    @model_validator(mode="after")
    def _window_pair(self) -> Self:
        if (self.start_time is None) != (self.end_time is None):
            raise ValueError(
                "start_time and end_time go together: give both or neither"
            )
        if self.start_time is not None and self.end_time <= self.start_time:
            raise ValueError("end_time must be after start_time")
        return self


class TopInCategoryByService(_Windowed):
    """Rank a field's values by log volume."""

    field: Annotated[
        CanonicalField,
        Field(description="Field to rank. Defaults to the emitting service."),
    ] = "service"
    category: SeverityFilter = None
    limit: Annotated[
        int, Field(ge=1, le=100, description="Most values to return (default 5).")
    ] = 5


class ComparePeriods(_DatasetScoped):
    """Compare counts between a baseline and a recent window."""

    baseline_start: Annotated[
        str,
        Field(
            pattern=DATETIME_REGEX,
            description="Baseline window start, 'YYYY-MM-DD HH:MM:SS' (UTC).",
        ),
    ]
    baseline_end: Annotated[
        str,
        Field(
            pattern=DATETIME_REGEX,
            description="Baseline window end, exclusive. After baseline_start.",
        ),
    ]
    recent_start: Annotated[
        str,
        Field(
            pattern=DATETIME_REGEX,
            description="Recent window start, 'YYYY-MM-DD HH:MM:SS' (UTC).",
        ),
    ]
    recent_end: Annotated[
        str,
        Field(
            pattern=DATETIME_REGEX,
            description="Recent window end, exclusive. After recent_start.",
        ),
    ]
    field: Annotated[
        CanonicalField,
        Field(description="Field whose values are compared. Defaults to service."),
    ] = "service"
    limit: Annotated[
        int, Field(ge=1, le=100, description="Most values to return (default 5).")
    ] = 5

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.baseline_end <= self.baseline_start:
            raise ValueError("baseline_end must be after baseline_start")
        if self.recent_end <= self.recent_start:
            raise ValueError("recent_end must be after recent_start")
        return self


class GetLogByLineId(_DatasetScoped):
    """Fetch one row by primary key.

    Only works on a corpus that has one. The raw OTel corpus does not, so this
    is refused there with a pointer to the datasets that do.

    A ``Line ID: n (dataset)`` reference in the user's message -- what the chat
    box inserts for a picked log line -- names both arguments: ``line_id=n`` and
    ``dataset``. Ids are only unique within one corpus.
    """

    line_id: Annotated[
        int,
        Field(
            ge=1,
            description=(
                "Primary-key id of the row to fetch. For a 'Line ID: n (dataset)' "
                "reference, pass n here and that dataset as `dataset`."
            ),
        ),
    ]


class GetTools(_Windowed):
    """Discover the real values a field takes, ranked by volume."""

    type: Annotated[
        CanonicalField,
        Field(
            description=(
                "Field whose values to list, ranked by row count so the most "
                "significant come first."
            )
        ),
    ]
    limit: Annotated[
        int, Field(ge=1, le=200, description="Most values to return (default 50).")
    ] = 50


class CountByDataset(BaseModel):
    """Whole-dataset severity counts across local datasets. No time window."""

    datasets: Annotated[
        list[LocalDatasetId] | None,
        Field(
            min_length=1,
            description=(
                "Local dataset ids to count, each counted over every row. "
                "Omit to count every local dataset. Only datasets held in a "
                "local table with exact counts are accepted."
            ),
        ),
    ] = None

    @field_validator("datasets")
    @classmethod
    def _local_datasets(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return value
        allowed = {d.id for d in local_datasets()}
        bad = [v for v in value if v not in allowed]
        if bad:
            raise ValueError(
                f"Cannot count {', '.join(bad)} whole. Choose from: "
                f"{', '.join(sorted(allowed))}; count a remote dataset with "
                "count_by_category_tool and a time window."
            )
        return list(dict.fromkeys(value))


class GetNearbyEvents(_DatasetScoped):
    """The log entries just before and after one anchor line.

    Neighbours are taken in record-id order, which is file order (and on
    OpenStack also time order), not a time-bounded slice. The two flags narrow
    the neighbours to one correlation handle; they do not move the anchor. The
    rows are examples for a timeline, never a frequency.
    """

    line_id: Annotated[
        int,
        Field(
            ge=1,
            description=(
                "Record id of the anchor line (a search hit's record_id). For a "
                "'Line ID: n (dataset)' reference, pass n here and that dataset "
                "as `dataset`."
            ),
        ),
    ]
    before: Annotated[
        int, Field(ge=0, le=50, description="Lines to return before the anchor.")
    ] = 5
    after: Annotated[
        int, Field(ge=0, le=50, description="Lines to return after the anchor.")
    ] = 5
    same_entity: Annotated[
        bool,
        Field(
            description=(
                "Only lines sharing the anchor's entity_id (process id on "
                "OpenStack, node on BGL)."
            )
        ),
    ] = False
    same_request: Annotated[
        bool,
        Field(
            description=(
                "Only lines sharing the anchor's request_id ('req-<uuid>'). "
                "Only OpenStack records a request id; other datasets refuse it."
            )
        ),
    ] = False


# --- Team counts --------------------------------------------------------------


class CountByTeam(BaseModel):
    """Log-entry counts per owning team and severity."""

    datasets: Annotated[
        list[LocalDatasetId] | None,
        Field(
            min_length=1,
            description=(
                "Local dataset ids to count. Omit to count every local dataset."
            ),
        ),
    ] = None
    start_time: Annotated[
        str | None,
        Field(
            pattern=DATETIME_REGEX,
            description=(
                "Window start, 'YYYY-MM-DD HH:MM:SS'. Optional; give it with "
                "end_time, or omit both to count every row."
            ),
        ),
    ] = None
    end_time: Annotated[
        str | None,
        Field(
            pattern=DATETIME_REGEX,
            description="Window end, exclusive. Must be after start_time.",
        ),
    ] = None
    category: SeverityFilter = None

    @field_validator("datasets")
    @classmethod
    def _local_datasets(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return value
        allowed = {d.id for d in local_datasets()}
        bad = [v for v in value if v not in allowed]
        if bad:
            raise ValueError(
                f"Cannot count {', '.join(bad)} by team. Choose from: "
                f"{', '.join(sorted(allowed))}."
            )
        return list(dict.fromkeys(value))

    @model_validator(mode="after")
    def _window(self) -> Self:
        if (self.start_time is None) != (self.end_time is None):
            raise ValueError("give start_time and end_time together, or neither")
        if self.start_time is not None and self.end_time <= self.start_time:
            raise ValueError("end_time must be after start_time")
        return self


class RankPriorities(BaseModel):
    """Rank problems in each dataset from measured counts in two windows.

    No number is accepted from the caller: the counts are read from SQL. A
    value's count is its baseline plus its recent count, so the two windows
    must not overlap -- they are halves of one timeline, added together.
    """

    datasets: Annotated[
        list[DatasetId] | None,
        Field(
            min_length=1,
            description=(
                "Datasets to rank, each ranked on its own. Omit to rank every "
                "local dataset. A remote dataset is ranked only with "
                "group_by='service' or 'template' (window at most 300 seconds, "
                "counts approximate)."
            ),
        ),
    ] = None
    group_by: Annotated[
        Literal["template", "service", "team"],
        Field(
            description=(
                "What a ranked row is: 'template' (a recurring event pattern), "
                "'service' (the emitting component) or 'team' (the owning team). "
                "Team ranking joins the ownership mapping and is local only."
            )
        ),
    ] = "service"
    baseline_start: Annotated[
        str,
        Field(
            pattern=DATETIME_REGEX,
            description=(
                "Baseline window start, 'YYYY-MM-DD HH:MM:SS' (UTC). Add a UTC "
                "offset ('+00') for a remote dataset."
            ),
        ),
    ]
    baseline_end: Annotated[
        str,
        Field(
            pattern=DATETIME_REGEX,
            description=(
                "Baseline window end, exclusive. After baseline_start and no "
                "later than recent_start."
            ),
        ),
    ]
    recent_start: Annotated[
        str,
        Field(
            pattern=DATETIME_REGEX,
            description=(
                "Recent window start, 'YYYY-MM-DD HH:MM:SS' (UTC). The counts of "
                "both windows are added together, so the windows must not "
                "overlap."
            ),
        ),
    ]
    recent_end: Annotated[
        str,
        Field(
            pattern=DATETIME_REGEX,
            description="Recent window end, exclusive. After recent_start.",
        ),
    ]
    limit: Annotated[
        int,
        Field(
            ge=1,
            le=50,
            description=(
                "Most ranked rows to return per dataset (default 10). The total "
                "number of ranked values is reported even when it exceeds this."
            ),
        ),
    ] = 10

    @field_validator("datasets")
    @classmethod
    def _known_datasets(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return value
        bad = [v for v in value if v not in dataset_ids()]
        if bad:
            raise ValueError(
                f"Unknown dataset(s) {', '.join(bad)}. Choose from: "
                f"{', '.join(dataset_ids())}."
            )
        return list(dict.fromkeys(value))

    @model_validator(mode="after")
    def _windows(self) -> Self:
        if self.baseline_end <= self.baseline_start:
            raise ValueError("baseline_end must be after baseline_start")
        if self.recent_end <= self.recent_start:
            raise ValueError("recent_end must be after recent_start")
        if self.baseline_end > self.recent_start:
            raise ValueError(
                "baseline_end must not be after recent_start: the two windows "
                "must not overlap, because their counts are added together"
            )
        if self.group_by == "team":
            local = {d.id for d in local_datasets()}
            remote = [v for v in (self.datasets or []) if v not in local]
            if remote:
                raise ValueError(
                    f"team ranking is local only; use group_by='service' for "
                    f"{', '.join(remote)}."
                )
        return self


class _Sweepable(_DatasetScoped):
    """Argument set for the sweep tools.

    Defaults to the remote corpus rather than the global default: only a
    chunk-addressable corpus can be swept, so defaulting to the small local
    demo table would make the common call an immediate refusal.
    """

    dataset: Annotated[
        str,
        _vocabulary(sorted(d.id for d in datasets_with(Capability.CHUNK_SWEEP))),
        Field(
            description=(
                "Which corpus to sweep. One of: "
                + ", ".join(sorted(d.id for d in datasets_with(Capability.CHUNK_SWEEP)))
                + "."
            ),
        ),
    ] = "otel_s3"

    @field_validator("dataset")
    @classmethod
    def _sweepable_dataset(cls, value: str) -> str:
        allowed = {d.id for d in datasets_with(Capability.CHUNK_SWEEP)}
        if value not in allowed:
            raise ValueError(
                f"Dataset '{value}' cannot be swept. Choose one of: "
                f"{', '.join(sorted(allowed))}."
            )
        return value


class SweepChunks(_Sweepable):
    """Graded pass over a whole remote corpus: map, profile, sample, rank.

    No time window, deliberately. Every other tool is bounded by one because it
    scans rows directly; this one addresses the corpus by row group and is
    bounded by its own budgets instead -- so asking it for a window would
    misrepresent what it does.
    """

    question: Annotated[
        str,
        Field(
            min_length=3,
            max_length=500,
            description=(
                "What the investigation is trying to establish. Its words are "
                "matched against sampled message text to rank chunks, so "
                "concrete terms work better than a restatement of the task."
            ),
        ),
    ]
    max_promotions: Annotated[
        int,
        Field(
            ge=0,
            le=4,
            description=(
                "Chunks to read message text for. Each costs about eleven "
                "minutes, so this is the expensive dial; 0 ranks without "
                "reading any text."
            ),
        ),
    ] = 0
    start_at: Annotated[
        int,
        Field(
            ge=0,
            description=(
                "Chunk index to resume from, taken from a previous sweep's "
                "cursor. Lets a second call continue through the corpus "
                "instead of re-examining its beginning."
            ),
        ),
    ] = 0
    confirm_cost: Annotated[
        bool,
        Field(
            description=(
                "Must be true to actually run. Called with false it returns a "
                "cost estimate only, so the user can agree to the spend first."
            )
        ),
    ] = False


class InvestigateChunk(_Sweepable):
    """Read one named chunk's message text and digest it.

    The companion to a sweep: the sweep says which chunks look relevant, this
    reads one properly. Expensive by nature, so it takes a single explicit id
    rather than a selection.
    """

    chunk_id: Annotated[
        str,
        Field(
            min_length=3,
            max_length=64,
            pattern=r"^[A-Za-z0-9_.]+:\d+-\d+$",
            description=(
                "Chunk identifier from a sweep result, such as "
                "'part_000:412-412'."
            ),
        ),
    ]
    question: Annotated[
        str,
        Field(max_length=500, description="What the investigation is after."),
    ] = ""


class OtelIndex(BaseModel):
    """Report what the in-place OTel index covers, or index a window.

    No ``dataset``: the tool manages exactly one object, the in-place index over
    the remote Parquet parts, so letting the model name a corpus would only
    invite it to point this at a corpus that has no such index.

    The window is optional. Given, it is the window to make searchable; omitted,
    the tool only reports what is covered today.
    """

    start_time: Annotated[
        str | None,
        Field(
            pattern=DATETIME_REGEX,
            description=(
                "Window start to make searchable, 'YYYY-MM-DD HH:MM:SS+00'. "
                "Give it together with end_time, or neither to only report "
                "current coverage."
            ),
        ),
    ] = None
    end_time: Annotated[
        str | None,
        Field(
            pattern=DATETIME_REGEX,
            description="Window end, exclusive. Required with start_time.",
        ),
    ] = None

    release: Annotated[
        bool,
        Field(
            description=(
                "Drop the index and return its disk. Use it only when the OTel "
                "corpus is finished with: the next search over an uncovered "
                "window rebuilds, which costs minutes. Cannot be combined with "
                "a window."
            )
        ),
    ] = False

    @model_validator(mode="after")
    def _window_pair(self) -> Self:
        if (self.start_time is None) != (self.end_time is None):
            raise ValueError(
                "start_time and end_time go together: give both or neither"
            )
        if self.start_time is not None and self.end_time <= self.start_time:
            raise ValueError("end_time must be after start_time")
        if self.release and self.start_time is not None:
            raise ValueError(
                "release drops the index; do not also ask for a window to be "
                "indexed in the same call"
            )
        return self
