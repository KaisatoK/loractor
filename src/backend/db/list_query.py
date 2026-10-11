import logging

from psycopg2 import sql

from backend.db.config import TABLE_NAME
from backend.db.connection import query_runner

logger = logging.getLogger(__name__)


@query_runner
def get_pids(table_name: str = TABLE_NAME) -> tuple[sql.Composable, list[str]]:
    logger.debug("Getting all pids")

    query = sql.SQL("""
            SELECT DISTINCT pid
            FROM {table_name}
            ORDER BY pid
        """).format(
        table_name=sql.Identifier(table_name),
    )

    params = []
    return query, params


@query_runner
def get_services(table_name: str = TABLE_NAME) -> tuple[sql.Composable, list[str]]:
    logger.debug("Getting all services")

    query = sql.SQL("""
            SELECT DISTINCT service
            FROM {table_name}
            ORDER BY service
        """).format(
        table_name=sql.Identifier(table_name),
    )

    params = []
    return query, params


@query_runner
def get_event_ids(table_name: str = TABLE_NAME) -> tuple[sql.Composable, list[str]]:
    logger.debug("Getting all event_ids")

    query = sql.SQL("""
            SELECT DISTINCT event_id
            FROM {table_name}
            ORDER BY event_id
        """).format(
        table_name=sql.Identifier(table_name),
    )

    params = []
    return query, params
