import psycopg2
from pathlib import Path


def get_conn():
    return psycopg2.connect(
        "host=localhost dbname=postgres user=postgres password=postgres"
    )


def import_from_csv(relative_file_path: str, table_name: str):
    conn = get_conn()
    cursor = conn.cursor()
    base_dir = Path(__file__).resolve().parent
    absolute_file_path = base_dir / relative_file_path

    try:
        create_table_query = f"""
            CREATE TABLE IF NOT EXISTS {table_name} (
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
                event_template TEXT
            );
        """

        cursor.execute(create_table_query)
        print(f"Table '{table_name}' created successfully.")

        copy_query = f"""
            COPY {table_name} (
                line_id, log_record, log_date, log_time, pid, 
                level, component, addr, content, event_id, event_template
            )
            FROM STDIN
            WITH CSV HEADER DELIMITER ','
        """

        with open(absolute_file_path, "r", encoding="utf-8") as f:
            cursor.copy_expert(copy_query, f)

        conn.commit()
        print("Import completed successfully.")
    except Exception as e:
        conn.rollback()
        print(e)
    finally:
        cursor.close()
        conn.close()


if __name__ == "__main__":
    import_from_csv("csv/OpenStack_2k.log_structured.csv", "LOG")
    

# --- 1. Count / aggregate ---
def count_by_category(hours: int, category: str | None = None) -> list[dict]:
    table = resolve_table(project)
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    query = sql.SQL("""
        SELECT category, COUNT(*) AS count
        FROM {table}
        WHERE date > NOW() - INTERVAL '%s hours'
        {category_filter}
        GROUP BY category
        ORDER BY count DESC
    """).format(
        table=table,
        category_filter=sql.SQL("AND category = %s") if category else sql.SQL("")
    )

    params = [hours] + ([category] if category else [])
    cur.execute(query, params)
    rows = cur.fetchall()
    conn.close()
    return rows


# --- 2. Trend over time ---
def get_trend(project: str, service: str, days: int) -> list[dict]:
    table = resolve_table(project)
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    query = sql.SQL("""
        SELECT DATE(date) AS day, COUNT(*) AS count
        FROM {table}
        WHERE service = %s
          AND date > NOW() - INTERVAL '%s days'
        GROUP BY DATE(date)
        ORDER BY day
    """).format(table=table)

    cur.execute(query, [service, days])
    rows = cur.fetchall()
    conn.close()
    return rows


# --- 3. Free-text search (SereneDB fuzzy/full-text) ---
def search_logs(project: str, query_text: str, limit: int = 10) -> list[dict]:
    table = resolve_table(project)
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    # illustrative syntax — confirm exact function names in SereneDB docs
    query = sql.SQL("""
        SELECT date, category, service, location, message,
               search_rank(message, %s) AS relevance
        FROM {table}
        WHERE fuzzy_match(message, %s)
        ORDER BY relevance DESC
        LIMIT %s
    """).format(table=table)

    cur.execute(query, [query_text, query_text, limit])
    rows = cur.fetchall()
    conn.close()
    return rows


# --- 4. Breakdown by location ---
def top_locations(project: str, hours: int, category: str | None = None, limit: int = 5) -> list[dict]:
    table = resolve_table(project)
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    query = sql.SQL("""
        SELECT location, COUNT(*) AS count
        FROM {table}
        WHERE date > NOW() - INTERVAL '%s hours'
        {category_filter}
        GROUP BY location
        ORDER BY count DESC
        LIMIT %s
    """).format(
        table=table,
        category_filter=sql.SQL("AND category = %s") if category else sql.SQL("")
    )

    params = [hours] + ([category] if category else []) + [limit]
    cur.execute(query, params)
    rows = cur.fetchall()
    conn.close()
    return rows


# --- 5. Compare two time windows ---
def compare_periods(project: str, service: str, recent_hours: int, baseline_hours_ago: int) -> dict:
    table = resolve_table(project)
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    query = sql.SQL("""
        SELECT
          COUNT(*) FILTER (WHERE date > NOW() - INTERVAL '%s hours') AS recent_count,
          COUNT(*) FILTER (
            WHERE date <= NOW() - INTERVAL '%s hours'
            AND date > NOW() - INTERVAL '%s hours'
          ) AS baseline_count
        FROM {table}
        WHERE service = %s
    """).format(table=table)

    cur.execute(query, [recent_hours, recent_hours, baseline_hours_ago, service])
    row = cur.fetchone()
    conn.close()
    return row
