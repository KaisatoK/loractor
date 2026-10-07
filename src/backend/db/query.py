from psycopg2 import sql

from backend.db.utils import query_runner

EMBEDDING_MODEL = "nomic-embed-text:v1.5"
EMBEDDING_DIMENSION = 768
TABLE_NAME = "log"


@query_runner
def count_by_category(
    start_time: str,
    end_time: str,
    table_name: str = TABLE_NAME,
    category: str | None = None,
) -> tuple[sql.Composable, list[str]]:
    print(f"Counting for category {category} from {start_time} to {end_time}")

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
def get_trend_by_category(
    start_time: str,
    end_time: str,
    pid: int,
    table_name: str = TABLE_NAME,
    category: str | None = None,
) -> tuple[sql.Composable, list[str | int]]:
    print(
        f"Getting minute-by-minute trend for pid {pid} and category {category} from {start_time} to {end_time}"
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
def search_logs(
    query_text: str,
    table_name: str = TABLE_NAME,
    category: str | None = None,
    limit: int = 10,
) -> tuple[sql.Composable, list[str | int]]:
    print(f'Getting top {limit} logs related to "{query_text}" for category {category}')

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
        SELECT t.log_timestamp, t.pid, t.category, t.component, t.content,
            SUM(1.0 / (60 + f.rank)) AS relevance
        FROM fused f
        JOIN {table_name} t ON t.line_id = f.line_id
        {category_filter}
        GROUP BY t.log_timestamp, t.pid, t.category, t.component, t.content
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
    print(
        f"Getting top {limit} service(s) for category {category} from {start_time} to {end_time}"
    )

    query = sql.SQL("""
        SELECT component, COUNT(*) AS count
        FROM {table_name}
        WHERE log_timestamp >= %s::timestamp
            AND log_timestamp <= %s::timestamp
        {category_filter}
        GROUP BY component
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
    print(
        f"Comparing top {limit} service between baseline ({baseline_start} -> {baseline_end}) and recent period ({recent_start} -> {recent_end})."
    )

    query = sql.SQL("""
        WITH baseline AS (
            SELECT component, category, COUNT(*) AS baseline_count
            FROM {table_name}
            WHERE log_timestamp >= %s::timestamp 
                AND log_timestamp <= %s::timestamp
            GROUP BY component, category
        ),
        recent AS (
            SELECT component, category, COUNT(*) AS recent_count
            FROM {table_name}
            WHERE log_timestamp >= %s::timestamp 
                AND log_timestamp <= %s::timestamp
            GROUP BY component, category
        )
        SELECT 
            COALESCE(r.component, b.component) AS component,
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
            ON r.component = b.component AND r.category = b.category
        ORDER BY volume_change DESC
        LIMIT %s
    """).format(table_name=sql.Identifier(table_name))

    params = [baseline_start, baseline_end, recent_start, recent_end, limit]

    return query, params


if __name__ == "__main__":

    def print_row(rows: list[dict]) -> None:
        for row in rows:
            print(row)

        print()

    ### COUNT LINES BY CATEGORY (INFO, WARNING, ERROR)
    # start = "2017-05-16 00:00:00"
    # end = "2017-05-16 00:01:00"
    # print_row(count_by_category(start, end))
    # print_row(count_by_category(start, end, category="INFO"))
    # print_row(count_by_category(start, end, category="WARNING"))
    # print_row(count_by_category(start, end, category="ERROR"))

    ### GET TREND OF A PID
    # start = "2017-05-16 00:00:00"
    # end = "2017-05-16 00:10:00"
    # print_row(get_trend_by_category(start, end, 25746))
    # print_row(get_trend_by_category(start, end, 2931, category="WARNING"))

    ### FULL-TEXT SEARCH LOG
    # print_row(search_logs("connection", category=None, limit=5))
    # print_row(search_logs("nework", category=None, limit=5))
    # print_row(search_logs("network", category="WARNING"))
    # print_row(search_logs("resource", category=None))

    ### GET TOP OF A CATEGORY OF A SERVICE
    # start = "2017-05-16 00:00:00"
    # end = "2017-05-16 00:01:00"
    # print_row(top_in_category_by_service(start, end, category=None))
    # print_row(top_in_category_by_service(start, end, category="INFO", limit=10))
    # print_row(top_in_category_by_service(start, end, category="ERROR", limit=2))

    ### COMPARE SERVICES FOR A BASELINE AND A RECENT TIME PERIOD
    # baseline_start = "2017-05-16 00:00:00"
    # baseline_end = "2017-05-16 00:01:00"
    # recent_start = "2017-05-16 00:02:00"
    # recent_end = "2017-05-16 00:03:00"
    # print_row(compare_periods(baseline_start, baseline_end, recent_start, recent_end))
