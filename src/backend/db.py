import psycopg2
import functools
import logging

from contextlib import contextmanager
from typing import Callable, ParamSpec
from psycopg2 import sql
from psycopg2.extras import RealDictCursor

from pathlib import Path

SERENEDB_PORT = 7890
MODEL_BASE_URL = "http://host.docker.internal:11434"
EMBEDDING_MODEL = "nomic-embed-text:v1.5"
EMBEDDING_DIMENSION = 768

logger = logging.getLogger(__name__)
P = ParamSpec("P")


def get_conn():
    return psycopg2.connect(
        f"host=localhost port={SERENEDB_PORT} dbname=postgres user=postgres password=postgres"
    )


@contextmanager
def db_cursor():
    conn = get_conn()
    cur = None
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        yield cur
    except Exception:
        conn.rollback()
        raise
    finally:
        if cur is not None:
            cur.close()
        conn.close()


def query_runner(
    func: Callable[P, tuple[sql.Composable, list]],
) -> Callable[P, list[dict]]:
    @functools.wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> list[dict]:
        logger.info("Running %s args=%s kwargs=%s", func.__name__, args, kwargs)

        query, params = func(*args, **kwargs)

        with db_cursor() as cur:
            cur.execute("SET sdb_ai_allow_insecure_endpoint = true")
            cur.execute(query, params)
            return [dict(row) for row in cur.fetchall()]

    return wrapper


def create_ai_secret():
    """Registers the local Ollama AI model provider secret in SereneDB."""
    conn = get_conn()
    cur = conn.cursor()

    try:
        cur.execute("DROP SECRET IF EXISTS local_ai")
        print("Dropped old secret 'local_ai' if it existed.")

        query = sql.SQL("""
            CREATE SECRET local_ai (
                TYPE openai,
                base_url {base_url},
                embeddings_path '/v1/embeddings',
                model {embedding_model}
            )
        """).format(
            base_url=sql.Literal(MODEL_BASE_URL),
            embedding_model=sql.Literal(EMBEDDING_MODEL),
        )
        cur.execute(query)
        conn.commit()
        print("AI secret 'local_ai' created successfully.")
    except Exception as e:
        conn.rollback()
        print(e)
    finally:
        cur.close()
        conn.close()


def import_from_csv(table_name: str, relative_file_path: str):
    conn = get_conn()
    cur = conn.cursor()
    base_dir = Path(__file__).resolve().parent
    absolute_file_path = base_dir / relative_file_path

    try:
        cur.execute("SET sdb_ai_allow_insecure_endpoint = true")

        drop_query = sql.SQL("DROP TABLE IF EXISTS {table_name} CASCADE").format(
            table_name=sql.Identifier(table_name)
        )
        cur.execute(drop_query)
        print(f"Dropped old table '{table_name}' if it existed.")

        create_table_query = sql.SQL("""
            CREATE TABLE {table_name} (
                line_id INT PRIMARY KEY,
                log_record TEXT,
                log_date DATE,
                log_time TIME,
                log_timestamp TIMESTAMP,
                pid INT,
                category VARCHAR(50),
                component TEXT,
                addr TEXT,
                content TEXT,
                event_id VARCHAR(50),
                event_template TEXT,
                embedding FLOAT[{embedding_dimension}]
            )
        """).format(
            table_name=sql.Identifier(table_name),
            embedding_dimension=sql.Literal(EMBEDDING_DIMENSION),
        )
        cur.execute(create_table_query)
        print(f"Table '{table_name}' created successfully.")

        copy_query = sql.SQL("""
            COPY {table_name} (
                line_id, log_record, log_date, log_time, pid, 
                category, component, addr, content, event_id, event_template
            )
            FROM STDIN
            WITH CSV HEADER DELIMITER ','
        """).format(table_name=sql.Identifier(table_name))
        with open(absolute_file_path, "r", encoding="utf-8") as f:
            cur.copy_expert(copy_query, f)
        print("CSV data copied successfully.")

        update_timestamp_query = sql.SQL("""
            UPDATE {table_name} 
            SET log_timestamp = (log_date + log_time)::timestamp
        """).format(table_name=sql.Identifier(table_name))
        cur.execute(update_timestamp_query)
        print("Populated log_timestamp column.")

        update_embedding_query = sql.SQL("""
            UPDATE {table_name} 
            SET embedding = ai_embed(content, {embedding_model}, 'local_ai')::FLOAT[{embedding_dimension}]
        """).format(
            table_name=sql.Identifier(table_name),
            embedding_model=sql.Literal(EMBEDDING_MODEL),
            embedding_dimension=sql.Literal(EMBEDDING_DIMENSION),
        )
        cur.execute(update_embedding_query)
        print("Generated embedding vectors.")

        cur.execute("""
            CREATE TEXT SEARCH DICTIONARY IF NOT EXISTS english AS 
            split_text(case := 'lower') WITH (frequency, position, offset)
        """)

        create_index_query = sql.SQL("""
            CREATE INDEX {index_name} ON {table_name} USING inverted (
                line_id, 
                content english, 
                embedding ivf (metric = 'cosine')
            )
        """).format(
            index_name=sql.Identifier(f"{table_name}_idx"),
            table_name=sql.Identifier(table_name),
        )
        cur.execute(create_index_query)

        refresh_query = sql.SQL("VACUUM (REFRESH_TABLE) {table_name}").format(
            table_name=sql.Identifier(table_name)
        )
        cur.execute(refresh_query)
        print("Created the text search dictionary and inverted index.")

        conn.commit()
        print("Import completed successfully.")
    except Exception as e:
        conn.rollback()
        print(e)
    finally:
        cur.close()
        conn.close()


@query_runner
def count_by_category(
    table_name: str, start_time: str, end_time: str, category: str | None = None
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
    table_name: str,
    start_time: str,
    end_time: str,
    pid: int,
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
    table_name: str, query_text: str, category: str | None = None, limit: int = 10
) -> tuple[sql.Composable, list[str | int]]:
    print(f'Getting top {limit} logs related to "{query_text}"')

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
        WHERE t.category = %s
        GROUP BY t.log_timestamp, t.pid, t.category, t.component, t.content
        ORDER BY SUM(1.0 / (60 + f.rank)) DESC
        LIMIT %s
    """).format(
        embedding_model=sql.Literal(EMBEDDING_MODEL),
        embedding_dimension=sql.Literal(EMBEDDING_DIMENSION),
        index_name=sql.Identifier(f"{table_name}_idx"),
        table_name=sql.Identifier(table_name),
    )

    params = [query_text, query_text, category, limit]
    return query, params


@query_runner
def top_in_category_by_service(
    table_name: str,
    start_time: str,
    end_time: str,
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
    table_name: str,
    baseline_start: str,
    baseline_end: str,
    recent_start: str,
    recent_end: str,
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
    table_name = "log"

    def print_row(rows: list[dict]) -> None:
        for row in rows:
            print(row)

        print()

    ### CREATE AI SECRET REQUIRED TO RUN SERENEDB, ONLY NEED TO BE RAN ONCE
    # create_ai_secret()

    ### IMPORT FROM CSV
    # import_from_csv(table_name, "csv/OpenStack_2k.log_structured.csv")

    ### COUNT LINES BY CATEGORY (INFO, WARNING, ERROR)
    # start = "2017-05-16 00:00:00"
    # end = "2017-05-16 00:01:00"
    # print_row(count_by_category(table_name, start, end))
    # print_row(count_by_category(table_name, start, end, "INFO"))
    # print_row(count_by_category(table_name, start, end, "WARNING"))
    # print_row(count_by_category(table_name, start, end, "ERROR"))

    ### GET TREND OF A PID
    # start = "2017-05-16 00:00:00"
    # end = "2017-05-16 00:10:00"
    # print_row(get_trend_by_category(table_name, start, end, 25746))
    # print_row(get_trend_by_category(table_name, start, end, 2931, "WARNING"))

    ### FULL-TEXT SEARCH LOG
    # print_row(search_logs(table_name, "connection", None, 5))
    # print_row(search_logs(table_name, "nework", None, 5))
    # print_row(search_logs(table_name, "network", "WARNING", 5))
    # print_row(search_logs(table_name, "resource", None, 5))

    ### GET TOP OF A CATEGORY OF A SERVICE
    # start = "2017-05-16 00:00:00"
    # end = "2017-05-16 00:01:00"
    # print_row(top_in_category_by_service(table_name, start, end, None))
    # print_row(top_in_category_by_service(table_name, start, end, "INFO", 10))
    # print_row(top_in_category_by_service(table_name, start, end, "ERROR", 2))

    ### COMPARE SERVICES FOR A BASELINE AND A RECENT TIME PERIOD
    # baseline_start = "2017-05-16 00:00:00"
    # baseline_end = "2017-05-16 00:01:00"
    # recent_start = "2017-05-16 00:02:00"
    # recent_end = "2017-05-16 00:03:00"
    # print_row(
    #     compare_periods(
    #         table_name, baseline_start, baseline_end, recent_start, recent_end
    #     )
    # )
