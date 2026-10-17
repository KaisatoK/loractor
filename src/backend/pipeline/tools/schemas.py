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

from backend.db.datasets import DEFAULT_DATASET_ID, dataset_ids

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


class _DatasetScoped(BaseModel):
    """Base for every tool argument set: a validated corpus id."""

    dataset: Annotated[
        str, Field(description=_DATASET_DESCRIPTION)
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
    category: Annotated[
        str | None,
        Field(
            description=(
                "Exact severity to filter on, in the corpus' own casing "
                "(OpenStack uses 'ERROR', the OTel corpus uses 'error')."
            )
        ),
    ] = None
    limit: Annotated[int, Field(ge=1, le=200)] = 50


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
    category: str | None = None
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
    category: str | None = None
    limit: Annotated[int, Field(ge=1, le=50)] = 10
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
    category: str | None = None
    limit: Annotated[int, Field(ge=1, le=100)] = 5


class ComparePeriods(_DatasetScoped):
    """Compare counts between a baseline and a recent window."""

    baseline_start: Annotated[str, Field(pattern=DATETIME_REGEX)]
    baseline_end: Annotated[str, Field(pattern=DATETIME_REGEX)]
    recent_start: Annotated[str, Field(pattern=DATETIME_REGEX)]
    recent_end: Annotated[str, Field(pattern=DATETIME_REGEX)]
    field: CanonicalField = "service"
    limit: Annotated[int, Field(ge=1, le=100)] = 5

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
    limit: Annotated[int, Field(ge=1, le=200)] = 50


class CountByDataset(BaseModel):
    """Whole-dataset severity counts across local datasets. No time window."""

    datasets: Annotated[
        list[str] | None,
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
        from backend.db.datasets import local_datasets

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


# One description for every transport: the local tool, the MCP wrapper and the
# MCP server all use it, so the model and an external MCP client read the same
# contract.
NEARBY_EVENTS_DESCRIPTION = (
    "Return the log entries just before and after one anchor line, for a "
    "before/after timeline. Inputs: dataset (a corpus with a record id: "
    "openstack or bgl), line_id (the anchor, e.g. a search hit's record_id), "
    "before (0-50, default 5), after (0-50, default 5), same_entity (only "
    "lines with the anchor's entity_id: process id on OpenStack, node on BGL; "
    "default false), same_request (only lines with the anchor's request id, "
    "OpenStack only, refused elsewhere; default false). Returns kind=context "
    "rows ordered by record_id: record_id, ts, severity, service, entity_id, "
    "request_id (OpenStack), message, template, offset (negative before the "
    "anchor, 0 for the anchor, positive after). Neighbours follow file order, "
    "not a time window. An unknown line_id returns status=success with empty "
    "data and a note, not an error. Context rows are examples, never a "
    "frequency."
)


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


# --- Planned tool (docs/tool_requests.md T3) ---------------------------------
# Schema and shared description only. The tool itself is a stub in
# ``pipeline/tools/planned_tools.py`` and is not registered with any transport
# yet; see AGENTS.md "Integrating new tools and tables".

COUNT_BY_TEAM_DESCRIPTION = (
    "Count log entries per owning team and severity, joining each row's "
    "service (Component) to the team ownership table. Inputs: datasets "
    "(optional list of local dataset ids; default every local dataset), "
    "start_time and end_time (optional, together; omit both to count every "
    "row), category (optional severity). Returns kind=aggregate rows {team, "
    "severity, count} ordered by count, computed in the database over every "
    "matching row. Services with no owner count as team 'unowned', so team "
    "totals equal the dataset totals. Counts are log entries, not incidents."
)


class CountByTeam(BaseModel):
    """Log-entry counts per owning team and severity (T3)."""

    datasets: Annotated[
        list[str] | None,
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
    category: Annotated[
        str | None,
        Field(description="Exact severity to filter on, in the corpus' own casing."),
    ] = None

    @field_validator("datasets")
    @classmethod
    def _local_datasets(cls, value: list[str] | None) -> list[str] | None:
        from backend.db.datasets import local_datasets

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


class _Sweepable(_DatasetScoped):
    """Argument set for the sweep tools.

    Defaults to the remote corpus rather than the global default: only a
    chunk-addressable corpus can be swept, so defaulting to the small local
    demo table would make the common call an immediate refusal.
    """

    dataset: Annotated[str, Field(description=_DATASET_DESCRIPTION)] = "otel_s3"

    @field_validator("dataset")
    @classmethod
    def _sweepable_dataset(cls, value: str) -> str:
        from backend.db.datasets import Capability, datasets_with

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
