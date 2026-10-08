from psycopg2 import sql

from backend.db.utils import (
    TABLE_NAME,
    query_runner,
)


@query_runner
def get_pid_trend_by_category(
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
def get_service_trend_by_category(
    start_time: str,
    end_time: str,
    service: str,
    table_name: str = TABLE_NAME,
    category: str | None = None,
) -> tuple[sql.Composable, list[str | int]]:
    print(
        f"Getting minute-by-minute trend for service {service} and category {category} from {start_time} to {end_time}"
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
    print(
        f"Getting minute-by-minute trend for event_id {event_id} and category {category} from {start_time} to {end_time}"
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


if __name__ == "__main__":
    start = "2017-05-16 00:00:00"
    end = "2017-05-16 00:10:00"

    def print_row(rows: list[dict]) -> None:
        for row in rows:
            print(row)
        print()

    ### GET TREND OF A PID
    print(get_pid_trend_by_category(start, end, 25746))
    # print_row(get_pid_trend_by_category(start, end, 2931, category="WARNING"))

    ### GET TREND OF A SERVICE
    # print_row(get_service_trend_by_category(start, end, "nova.compute.resource_tracker"))
    # print_row(get_service_trend_by_category(start, end, "nova.virt.libvirt.imagecache", category="WARNING"))

    ### GET TREND OF AN EVENT ID
    # print_row(get_event_id_trend_by_category(start, end, "E35"))
    # print_row(get_event_id_trend_by_category(start, end, "E42", category="WARNING"))
