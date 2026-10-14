"""Database access layer: connection, configuration, and log queries."""

from backend.db.auto_complete import auto_complete
from backend.db.config import (
    EMBEDDING_DIMENSION,
    EMBEDDING_MODEL,
    LOG_SCHEMA,
    MODEL_BASE_URL,
    SERENEDB_PORT,
    TABLE_NAME,
)
from backend.db.connection import db_cursor, get_conn, query_runner
from backend.db.count_query import (
    count_by_category,
    count_event_id_by_category,
    count_pid_by_category,
    count_service_by_category,
)
from backend.db.list_query import get_event_ids, get_pids, get_services
from backend.db.query import (
    compare_periods,
    search_logs,
    top_in_category_by_service,
)
from backend.db.trend_query import (
    get_event_id_trend_by_category,
    get_pid_trend_by_category,
    get_service_trend_by_category,
)

__all__ = [
    "EMBEDDING_DIMENSION",
    "EMBEDDING_MODEL",
    "LOG_SCHEMA",
    "MODEL_BASE_URL",
    "SERENEDB_PORT",
    "TABLE_NAME",
    "auto_complete",
    "compare_periods",
    "count_by_category",
    "count_event_id_by_category",
    "count_pid_by_category",
    "count_service_by_category",
    "db_cursor",
    "get_conn",
    "get_event_id_trend_by_category",
    "get_event_ids",
    "get_pid_trend_by_category",
    "get_pids",
    "get_service_trend_by_category",
    "get_services",
    "query_runner",
    "search_logs",
    "top_in_category_by_service",
]
