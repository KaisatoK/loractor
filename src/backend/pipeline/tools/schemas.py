from typing import Annotated, Literal, Self

from pydantic import BaseModel, Field, model_validator

from backend.db.config import TABLE_NAME

DATETIME_REGEX = r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$"


class CountByCategory(BaseModel):
    start_time: Annotated[
        str,
        Field(
            pattern=DATETIME_REGEX,
            description="Timestamp in YYYY-MM-DD HH:MM:SS format.",
        ),
    ]
    end_time: Annotated[
        str,
        Field(
            pattern=DATETIME_REGEX,
            description="Timestamp in YYYY-MM-DD HH:MM:SS format.",
        ),
    ]
    type: Annotated[
        Literal["pid", "service", "event_id", "general"],
        Field(
            description="The type of count to perform. Must be one of 'pid', 'service', 'event_id', or 'general'.",
        ),
    ]
    table_name: str = TABLE_NAME
    category: Literal["INFO", "WARNING", "ERROR"] | None = None


class GetTrend(BaseModel):
    start_time: Annotated[
        str,
        Field(
            pattern=DATETIME_REGEX,
            description="Timestamp in YYYY-MM-DD HH:MM:SS format.",
        ),
    ]
    end_time: Annotated[
        str,
        Field(
            pattern=DATETIME_REGEX,
            description="Timestamp in YYYY-MM-DD HH:MM:SS format. Must be AFTER start_time, not equal to it.",
        ),
    ]
    type: Annotated[
        tuple[Literal["pid", "service", "event_id"], str],
        Field(
            description=(
                "The first element dictates the type of trend to retrieve. Must be one of 'pid', 'service', or 'event_id'."
                "The second element dictates the exact pid, service or event_id corresponds to the first element"
            ),
        ),
    ]
    table_name: str = TABLE_NAME
    category: str | None = None

    @model_validator(mode="after")
    def check_duration(self) -> Self:
        if self.end_time <= self.start_time:
            raise ValueError("end_time must be after start_time")
        return self


class SearchLogs(BaseModel):
    query_text: Annotated[
        str,
        Field(
            min_length=1,
            max_length=500,
            description="Text or concept to search for in log messages. Do not include INFO, WARNING, ERROR (or any case variations like info, Warning, Err) merely as severity filters.",
        ),
    ]
    category: str | None = None
    table_name: str = TABLE_NAME
    limit: Annotated[
        int,
        Field(
            ge=1,
            le=50,
            description="Maximum number of log entries to return. Must be between 1 and 50.",
        ),
    ] = 10


class TopInCategoryByService(BaseModel):
    start_time: Annotated[
        str,
        Field(
            pattern=DATETIME_REGEX,
            description="Timestamp in YYYY-MM-DD HH:MM:SS format.",
        ),
    ]
    end_time: Annotated[
        str,
        Field(
            pattern=DATETIME_REGEX,
            description="Timestamp in YYYY-MM-DD HH:MM:SS format.",
        ),
    ]
    table_name: str = TABLE_NAME
    category: str | None = None
    limit: Annotated[
        int,
        Field(
            ge=1,
            le=100,
            description="Number of top services to return. Must be between 1 and 100.",
        ),
    ] = 5


class ComparePeriods(BaseModel):
    baseline_start: Annotated[
        str,
        Field(
            pattern=DATETIME_REGEX,
            description="Timestamp in YYYY-MM-DD HH:MM:SS format.",
        ),
    ]
    baseline_end: Annotated[
        str,
        Field(
            pattern=DATETIME_REGEX,
            description="Timestamp in YYYY-MM-DD HH:MM:SS format.",
        ),
    ]
    recent_start: Annotated[
        str,
        Field(
            pattern=DATETIME_REGEX,
            description="Timestamp in YYYY-MM-DD HH:MM:SS format.",
        ),
    ]
    recent_end: Annotated[
        str,
        Field(
            pattern=DATETIME_REGEX,
            description="Timestamp in YYYY-MM-DD HH:MM:SS format.",
        ),
    ]
    table_name: str = TABLE_NAME
    limit: Annotated[
        int,
        Field(
            ge=1,
            le=100,
            description="Maximum number of log entries to return. Must be between 1 and 100.",
        ),
    ] = 5


class GetLogByLineId(BaseModel):
    line_id: Annotated[
        int,
        Field(
            ge=1,
            description="Primary-key line_id of the log row to fetch.",
        ),
    ]
    table_name: str = TABLE_NAME


class GetTools(BaseModel):
    type: Annotated[
        Literal["pid", "service", "event_id"],
        Field(
            description="The type of tool to invoke. Must be one of 'pid', 'service', or 'event_id'.",
        ),
    ]
    table_name: str = TABLE_NAME
