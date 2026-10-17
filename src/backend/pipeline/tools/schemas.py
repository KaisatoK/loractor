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
Bucket = Literal["second", "minute", "hour", "day"]


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
        Literal["general", "service", "entity_id", "event_id"],
        Field(
            description=(
                "Breakdown dimension. 'general' counts by severity only; the "
                "others add that field. 'entity_id' is the correlation handle "
                "(process id on OpenStack, trace id on OTel)."
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
        Bucket,
        Field(
            description=(
                "Bucket width. Replaces the old minute-only behaviour, so a "
                "trend can span seconds or days."
            )
        ),
    ] = "minute"
    category: str | None = None


class SearchLogs(_DatasetScoped):
    """Full-text search over an indexed corpus.

    No time window: this reads the inverted index, whose size is already bounded
    by whatever built it, so there is no scan to constrain.
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
    """

    line_id: Annotated[
        int,
        Field(ge=1, description="Primary-key id of the row to fetch."),
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
