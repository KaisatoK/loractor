import logging

from psycopg2 import sql

from backend.db.config import EMBEDDING_DIMENSION, EMBEDDING_MODEL, TABLE_NAME
from backend.db.connection import query_runner

logger = logging.getLogger(__name__)


@query_runner
def search_logs(
    query_text: str,
    table_name: str = TABLE_NAME,
    category: str | None = None,
    limit: int = 10,
) -> tuple[sql.Composable, list[str | int]]:
    logger.debug(
        'Getting top %s logs related to "%s" for category %s',
        limit,
        query_text,
        category,
    )

    query = sql.SQL("""
        WITH query_embedding AS (
            SELECT ai_embed(%s, {embedding_model}, 'local_ai')::FLOAT[{embedding_dimension}] AS emb
        ),
        fused AS (
            SELECT line_id,
                ROW_NUMBER() OVER (ORDER BY s DESC) AS rank 
            FROM (
                SELECT line_id,
                    BM25({index_name}.tableoid) AS s
                FROM {index_name}
                WHERE content @@ ts_levenshtein(%s, 1)
                ORDER BY s DESC 
                LIMIT 100
            ) lex
            UNION ALL
            SELECT line_id,
                ROW_NUMBER() OVER (ORDER BY dist) AS rank 
            FROM (
                SELECT line_id,
                    embedding <=> q.emb AS dist
                FROM {index_name}, query_embedding q
                ORDER BY dist 
                LIMIT 100
            ) vec
        )
        SELECT t.log_timestamp, t.pid, t.category, t.service, t.content, t.event_id,
            SUM(1.0 / (60 + f.rank)) AS relevance
        FROM fused f
        JOIN {table_name} t ON t.line_id = f.line_id
        {category_filter}
        GROUP BY t.log_timestamp, t.pid, t.category, t.service, t.content, t.event_id
        ORDER BY SUM(1.0 / (60 + f.rank)) DESC
        LIMIT %s
    """).format(
        embedding_model=sql.Literal(EMBEDDING_MODEL),
        embedding_dimension=sql.Literal(EMBEDDING_DIMENSION),
        index_name=sql.Identifier(f"{table_name}_idx"),
        table_name=sql.Identifier(table_name),
        category_filter=sql.SQL("WHERE t.category = %s") if category else sql.SQL(""),
    )

    params = [query_text, query_text] + ([category] if category else []) + [limit]
    return query, params


@query_runner
def top_in_category_by_service(
    start_time: str,
    end_time: str,
    table_name: str = TABLE_NAME,
    category: str | None = None,
    limit: int = 5,
) -> tuple[sql.Composable, list[str | int]]:
    logger.debug(
        "Getting top %s service(s) for category %s from %s to %s",
        limit,
        category,
        start_time,
        end_time,
    )

    query = sql.SQL("""
        SELECT service, COUNT(*) AS count
        FROM {table_name}
        WHERE log_timestamp >= %s::timestamp
            AND log_timestamp <= %s::timestamp
        {category_filter}
        GROUP BY service
        ORDER BY count DESC
        LIMIT %s
    """).format(
        table_name=sql.Identifier(table_name),
        category_filter=sql.SQL("AND category = %s") if category else sql.SQL(""),
    )

    params = [start_time, end_time] + ([category] if category else []) + [limit]
    return query, params


@query_runner
def compare_periods(
    baseline_start: str,
    baseline_end: str,
    recent_start: str,
    recent_end: str,
    table_name: str = TABLE_NAME,
    limit: int = 5,
) -> tuple[sql.Composable, list[str | int]]:
    logger.debug(
        "Comparing top %s service between baseline (%s -> %s) and recent period (%s -> %s).",
        limit,
        baseline_start,
        baseline_end,
        recent_start,
        recent_end,
    )

    query = sql.SQL("""
        WITH baseline AS (
            SELECT service, category, COUNT(*) AS baseline_count
            FROM {table_name}
            WHERE log_timestamp >= %s::timestamp 
                AND log_timestamp <= %s::timestamp
            GROUP BY service, category
        ),
        recent AS (
            SELECT service, category, COUNT(*) AS recent_count
            FROM {table_name}
            WHERE log_timestamp >= %s::timestamp 
                AND log_timestamp <= %s::timestamp
            GROUP BY service, category
        )
        SELECT 
            COALESCE(r.service, b.service) AS service,
            COALESCE(r.category, b.category) AS category,
            COALESCE(b.baseline_count, 0) AS baseline_count,
            COALESCE(r.recent_count, 0) AS recent_count,
            (COALESCE(r.recent_count, 0) - COALESCE(b.baseline_count, 0)) AS volume_change,
            ROUND(
                (COALESCE(r.recent_count, 0) - COALESCE(b.baseline_count, 0)) * 100.0 / 
                NULLIF(b.baseline_count, 0), 2
            ) AS percentage_change
        FROM recent r
        FULL OUTER JOIN baseline b 
            ON r.service = b.service AND r.category = b.category
        ORDER BY volume_change DESC
        LIMIT %s
    """).format(table_name=sql.Identifier(table_name))

    params = [baseline_start, baseline_end, recent_start, recent_end, limit]

    return query, params
