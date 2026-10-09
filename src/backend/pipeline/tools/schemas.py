from typing import Annotated, Any, Literal, TypedDict

from pydantic import BaseModel, Field

TABLE_NAME = "log"
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
    table_name: str = TABLE_NAME
    category: str | None = None


class GetTrendPID(BaseModel):
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
    pid: Annotated[
        int,
        Field(
            ge=0,
            description="Exact process ID. The value must come from the user or from previously collected evidence.",
        ),
    ]
    table_name: str = TABLE_NAME
    category: str | None = None


class GetTrendService(BaseModel):
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
    service: Annotated[
        str,
        Field(
            min_length=1,
            description="Exact service name. The value must come from the user or from previously collected evidence.",
        ),
    ]
    table_name: str = TABLE_NAME
    category: str | None = None


class GetTrendEvent(BaseModel):
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
    event_id: Annotated[
        str,
        Field(
            min_length=1,
            description="Exact event ID. The value must come from the user or from previously collected evidence.",
        ),
    ]
    table_name: str = TABLE_NAME
    category: str | None = None


class SearchLogs(BaseModel):
    query_text: Annotated[
        str,
        Field(
            min_length=1,
            max_length=500,
            description=(
                "Text or concept to search for in log messages. "
                "Do not include INFO, WARNING, ERROR (or any case variations like info, Warning, Err) "
                "merely as severity filters. Do not include empty strings, whitespace-only strings, or strings that are only punctuation. "
            ),
        ),
    ]
    category: str | None = None
    table_name: str = TABLE_NAME
    limit: Annotated[
        int,
        Field(
            ge=1,
            le=1000,
            description="Maximum number of log entries to return. Must be between 1 and 1000.",
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


class GetTools(BaseModel):
    table_name: str = TABLE_NAME
