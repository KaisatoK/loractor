import logging

from psycopg2 import sql

from backend.db.config import TABLE_NAME
from backend.db.connection import query_runner

logger = logging.getLogger(__name__)


@query_runner
def get_pid_trend_by_category(
    start_time: str,
    end_time: str,
    pid: int,
    table_name: str = TABLE_NAME,
    category: str | None = None,
) -> tuple[sql.Composable, list[str | int]]:
    logger.debug(
        "Getting minute-by-minute trend for pid %s and category %s from %s to %s",
        pid,
        category,
        start_time,
        end_time,
    )

    query = sql.SQL("""
        WITH minute_counts AS (
            SELECT 
                EXTRACT(MINUTE FROM log_time) AS minute, 
                COUNT(*) AS count
            FROM {table_name}
            WHERE pid = %s
                AND log_timestamp >= %s::timestamp
                AND log_timestamp <= %s::timestamp
                {category_filter}
            GROUP BY minute
        )
        SELECT 
            minute,
            count,
            LAG(count) OVER (ORDER BY minute) AS prev_count,
            CASE 
                WHEN LAG(count) OVER (ORDER BY minute) IS NULL THEN 'initial'
                WHEN count > LAG(count) OVER (ORDER BY minute) THEN 'up'
                WHEN count < LAG(count) OVER (ORDER BY minute) THEN 'down'
                ELSE 'flat'
            END AS trend
        FROM minute_counts
        ORDER BY minute
    """).format(
        table_name=sql.Identifier(table_name),
        category_filter=sql.SQL("AND category = %s") if category else sql.SQL(""),
    )

    params = [pid, start_time, end_time] + ([category] if category else [])
    return query, params


@query_runner
def get_service_trend_by_category(
    start_time: str,
    end_time: str,
    service: str,
    table_name: str = TABLE_NAME,
    category: str | None = None,
) -> tuple[sql.Composable, list[str | int]]:
    logger.debug(
        "Getting minute-by-minute trend for service %s and category %s from %s to %s",
        service,
        category,
        start_time,
        end_time,
    )

    query = sql.SQL("""
        WITH minute_counts AS (
            SELECT 
                EXTRACT(MINUTE FROM log_time) AS minute, 
                COUNT(*) AS count
            FROM {table_name}
            WHERE service = %s
                AND log_timestamp >= %s::timestamp
                AND log_timestamp <= %s::timestamp
                {category_filter}
            GROUP BY minute
        )
        SELECT 
            minute,
            count,
            LAG(count) OVER (ORDER BY minute) AS prev_count,
            CASE 
                WHEN LAG(count) OVER (ORDER BY minute) IS NULL THEN 'initial'
                WHEN count > LAG(count) OVER (ORDER BY minute) THEN 'up'
                WHEN count < LAG(count) OVER (ORDER BY minute) THEN 'down'
                ELSE 'flat'
            END AS trend
        FROM minute_counts
        ORDER BY minute
    """).format(
        table_name=sql.Identifier(table_name),
        category_filter=sql.SQL("AND category = %s") if category else sql.SQL(""),
    )

    params = [service, start_time, end_time] + ([category] if category else [])
    return query, params


@query_runner
def get_event_id_trend_by_category(
    start_time: str,
    end_time: str,
    event_id: str,
    table_name: str = TABLE_NAME,
    category: str | None = None,
) -> tuple[sql.Composable, list[str | int]]:
    logger.debug(
        "Getting minute-by-minute trend for event_id %s and category %s from %s to %s",
        event_id,
        category,
        start_time,
        end_time,
    )

    query = sql.SQL("""
        WITH minute_counts AS (
            SELECT 
                EXTRACT(MINUTE FROM log_time) AS minute, 
                COUNT(*) AS count
            FROM {table_name}
            WHERE event_id = %s
                AND log_timestamp >= %s::timestamp
                AND log_timestamp <= %s::timestamp
                {category_filter}
            GROUP BY minute
        )
        SELECT 
            minute,
            count,
            LAG(count) OVER (ORDER BY minute) AS prev_count,
            CASE 
                WHEN LAG(count) OVER (ORDER BY minute) IS NULL THEN 'initial'
                WHEN count > LAG(count) OVER (ORDER BY minute) THEN 'up'
                WHEN count < LAG(count) OVER (ORDER BY minute) THEN 'down'
                ELSE 'flat'
            END AS trend
        FROM minute_counts
        ORDER BY minute
    """).format(
        table_name=sql.Identifier(table_name),
        category_filter=sql.SQL("AND category = %s") if category else sql.SQL(""),
    )

    params = [event_id, start_time, end_time] + ([category] if category else [])
    return query, params
