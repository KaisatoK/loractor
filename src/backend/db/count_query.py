import logging

from psycopg2 import sql

from backend.db.config import TABLE_NAME
from backend.db.connection import query_runner

logger = logging.getLogger(__name__)


@query_runner
def count_by_category(
    start_time: str,
    end_time: str,
    table_name: str = TABLE_NAME,
    category: str | None = None,
) -> tuple[sql.Composable, list[str]]:
    logger.debug("Counting for category %s from %s to %s", category, start_time, end_time)

    query = sql.SQL("""
        SELECT category, COUNT(*) AS count
        FROM {table_name}
        WHERE log_timestamp >= %s::timestamp
            AND log_timestamp <= %s::timestamp
            {category_filter}
        GROUP BY category
        ORDER BY count DESC
    """).format(
        table_name=sql.Identifier(table_name),
        category_filter=sql.SQL("AND category = %s") if category else sql.SQL(""),
    )

    params = [start_time, end_time] + ([category] if category else [])
    return query, params


@query_runner
def count_pid_by_category(
    start_time: str,
    end_time: str,
    table_name: str = TABLE_NAME,
    category: str | None = None,
) -> tuple[sql.Composable, list[str]]:
    logger.debug("Counting pid for category %s from %s to %s", category, start_time, end_time)

    query = sql.SQL("""
        SELECT pid, category, COUNT(*) AS count
        FROM {table_name}
        WHERE log_timestamp >= %s::timestamp
            AND log_timestamp <= %s::timestamp
            {category_filter}
        GROUP BY pid, category
        ORDER BY pid, count DESC
    """).format(
        table_name=sql.Identifier(table_name),
        category_filter=sql.SQL("AND category = %s") if category else sql.SQL(""),
    )

    params = [start_time, end_time] + ([category] if category else [])
    return query, params


@query_runner
def count_service_by_category(
    start_time: str,
    end_time: str,
    table_name: str = TABLE_NAME,
    category: str | None = None,
) -> tuple[sql.Composable, list[str]]:
    logger.debug("Counting service for category %s from %s to %s", category, start_time, end_time)

    query = sql.SQL("""
        SELECT service, category, COUNT(*) AS count
        FROM {table_name}
        WHERE log_timestamp >= %s::timestamp
            AND log_timestamp <= %s::timestamp
            {category_filter}
        GROUP BY service, category
        ORDER BY service, count DESC
    """).format(
        table_name=sql.Identifier(table_name),
        category_filter=sql.SQL("AND category = %s") if category else sql.SQL(""),
    )

    params = [start_time, end_time] + ([category] if category else [])
    return query, params


@query_runner
def count_event_id_by_category(
    start_time: str,
    end_time: str,
    table_name: str = TABLE_NAME,
    category: str | None = None,
) -> tuple[sql.Composable, list[str]]:
    logger.debug("Counting event_id for category %s from %s to %s", category, start_time, end_time)

    query = sql.SQL("""
        SELECT event_id, category, COUNT(*) AS count
        FROM {table_name}
        WHERE log_timestamp >= %s::timestamp
            AND log_timestamp <= %s::timestamp
            {category_filter}
        GROUP BY event_id, category
        ORDER BY event_id, count DESC
    """).format(
        table_name=sql.Identifier(table_name),
        category_filter=sql.SQL("AND category = %s") if category else sql.SQL(""),
    )

    params = [start_time, end_time] + ([category] if category else [])
    return query, params
