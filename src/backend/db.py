import psycopg2

from psycopg2 import sql
from psycopg2.extras import RealDictCursor, RealDictRow

from pathlib import Path


def get_conn():
    return psycopg2.connect(
        "host=localhost port=7890 dbname=postgres user=postgres password=postgres"
    )


def create_ai_secret():
    """Registers the local Ollama AI model provider secret in SereneDB."""
    conn = get_conn()
    cur = conn.cursor()

    try:
        cur.execute("DROP SECRET IF EXISTS local_ai")
        print("Dropped old secret 'local_ai' if it existed.")

        cur.execute("""
            CREATE SECRET local_ai (
                TYPE openai,
                base_url 'http://host.docker.internal:11434',
                embeddings_path '/v1/embeddings',
                model 'all-minilm'
            )
        """)
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
                pid INT,
                level VARCHAR(50),
                component TEXT,
                addr TEXT,
                content TEXT,
                event_id VARCHAR(50),
                event_template TEXT,
                embedding FLOAT[384]
            )
        """).format(table_name=sql.Identifier(table_name))

        cur.execute(create_table_query)
        print(f"Table '{table_name}' created successfully.")

        copy_query = sql.SQL("""
            COPY {table_name} (
                line_id, log_record, log_date, log_time, pid, 
                level, component, addr, content, event_id, event_template
            )
            FROM STDIN
            WITH CSV HEADER DELIMITER ','
        """).format(table_name=sql.Identifier(table_name))

        with open(absolute_file_path, "r", encoding="utf-8") as f:
            cur.copy_expert(copy_query, f)

        print("CSV data copied successfully.")

        update_embedding_query = sql.SQL("""
            UPDATE {table_name} 
            SET embedding = ai_embed(content, 'all-minilm', 'local_ai')::FLOAT[384]
        """).format(table_name=sql.Identifier(table_name))

        cur.execute(update_embedding_query)
        print("Generated embedding vectors.")

        cur.execute("""
            CREATE TEXT SEARCH DICTIONARY IF NOT EXISTS english AS 
            split_text(case := 'lower') WITH (frequency, position, offset)
        """)
        cur.execute("""
            CREATE INDEX log_idx ON log USING inverted (
                line_id, 
                content english, 
                embedding ivf (metric = 'cosine')
            )
        """)
        cur.execute("VACUUM (REFRESH_TABLE) log")
        print("Created the text search dictionary and inverted index.")

        conn.commit()
        print("Import completed successfully.")
    except Exception as e:
        conn.rollback()
        print(e)
    finally:
        cur.close()
        conn.close()


def count_by_category(
    table_name: str, start_time: str, end_time: str, category: str | None = None
) -> list[RealDictRow]:
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    print(f"Counting for category {category} from {start_time} to {end_time}")

    try:
        query = sql.SQL("""
            SELECT level, COUNT(*) AS count
            FROM {table_name}
            WHERE (log_date + log_time) >= %s::timestamp
              AND (log_date + log_time) <= %s::timestamp
            {category_filter}
            GROUP BY level
            ORDER BY count DESC
        """).format(
            table_name=sql.Identifier(table_name),
            category_filter=sql.SQL("AND level = %s") if category else sql.SQL(""),
        )

        params = [start_time, end_time] + ([category] if category else [])
        cur.execute(query, params)
        return cur.fetchall()
    except Exception as e:
        conn.rollback()
        print(e)
        return []
    finally:
        cur.close()
        conn.close()


def get_trend(
    table_name: str, start_time: str, end_time: str, pid: int
) -> list[RealDictRow]:
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    print(f"Getting trend for pid {pid} in each minute from {start_time} to {end_time}")

    try:
        query = sql.SQL("""
            SELECT EXTRACT(MINUTE FROM log_time) AS minute, COUNT(*) AS count
            FROM {table_name}
            WHERE pid = %s
              AND (log_date + log_time) >= %s::timestamp
              AND (log_date + log_time) <= %s::timestamp
            GROUP BY minute
            ORDER BY minute
        """).format(table_name=sql.Identifier(table_name))

        cur.execute(query, [pid, start_time, end_time])
        return cur.fetchall()
    except Exception as e:
        conn.rollback()
        print(e)
        return []
    finally:
        cur.close()
        conn.close()


def search_logs(table_name: str, query_text: str, limit: int = 10) -> list[RealDictRow]:
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    print(f'Getting top {limit} logs related to "{query_text}"')

    try:
        cur.execute("SET sdb_ai_allow_insecure_endpoint = true")

        query = """
            SELECT log_date, log_time, pid, level, component, content,
                   (1.0 - (embedding <=> ai_embed(%s, 'all-minilm', 'local_ai')::FLOAT[384])) AS relevance
            FROM log_idx
            WHERE content @@ %s
            ORDER BY embedding <=> ai_embed(%s, 'all-minilm', 'local_ai')::FLOAT[384] ASC
            LIMIT %s
        """

        cur.execute(query, [query_text, query_text, query_text, limit])
        return cur.fetchall()
    except Exception as e:
        conn.rollback()
        print(e)
        return []
    finally:
        cur.close()
        conn.close()


def top_in_category_by_service(
    table_name: str,
    start_time: str,
    end_time: str,
    category: str | None = None,
    limit: int = 5,
) -> list[RealDictRow]:
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    print(
        f"Getting top {limit} service(s) for category {category} from {start_time} to {end_time}"
    )

    try:
        query = sql.SQL("""
            SELECT component, COUNT(*) AS count
            FROM {table_name}
            WHERE (log_date + log_time) >= %s::timestamp
              AND (log_date + log_time) <= %s::timestamp
            {level_filter}
            GROUP BY component
            ORDER BY count DESC
            LIMIT %s
        """).format(
            table_name=sql.Identifier(table_name),
            level_filter=sql.SQL("AND level = %s") if category else sql.SQL(""),
        )

        params = [start_time, end_time] + ([category] if category else []) + [limit]
        cur.execute(query, params)
        return cur.fetchall()
    except Exception as e:
        conn.rollback()
        print(e)
        return []
    finally:
        cur.close()
        conn.close()


def compare_periods(
    table_name: str,
    service: str,
    baseline_start_time: str,
    baseline_end_time: str,
    recent_start_time: str,
    recent_end_time: str,
) -> RealDictRow | None:
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    print(
        f"Comparing service {service} for "
        f"baseline {baseline_start_time} -> {baseline_end_time} and "
        f"recent {recent_start_time} -> {recent_end_time}"
    )

    try:
        query = sql.SQL("""
            SELECT
            COUNT(*) FILTER (
                WHERE (log_date + log_time) >= %s::timestamp
                  AND (log_date + log_time) <= %s::timestamp
            ) AS baseline_count,
            COUNT(*) FILTER (
                WHERE (log_date + log_time) >= %s::timestamp
                  AND (log_date + log_time) <= %s::timestamp
            ) AS recent_count
            FROM {table_name}
            WHERE component = %s
        """).format(table_name=sql.Identifier(table_name))

        cur.execute(
            query,
            [
                baseline_start_time,
                baseline_end_time,
                recent_start_time,
                recent_end_time,
                service,
            ],
        )
        return cur.fetchone()
    except Exception as e:
        conn.rollback()
        print(e)
        return None
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    table_name = "log"

    def print_row(rows: list[RealDictRow]) -> None:
        for row in rows:
            print(row)

        print()

    ### CREATE AI SECRET REQUIRED TO RUN SERENEDB
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
    # print_row(get_trend(table_name, start, end, 25746))

    ### FULL-TEXT SEARCH LOG
    print_row(search_logs(table_name, "network failure", 5))
    print_row(search_logs(table_name, "resource"))

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
    # print(compare_periods(table_name, "nova.virt.libvirt.driver", baseline_start, baseline_end, recent_start, recent_end))
