from psycopg2 import sql

from backend.db.utils import (
    TABLE_NAME,
    query_runner,
)


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
def count_pid_by_category(
    start_time: str,
    end_time: str,
    table_name: str = TABLE_NAME,
    category: str | None = None,
) -> tuple[sql.Composable, list[str]]:
    print(f"Counting pid for category {category} from {start_time} to {end_time}")

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
    print(f"Counting service for category {category} from {start_time} to {end_time}")

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
    print(f"Counting event_id for category {category} from {start_time} to {end_time}")

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


if __name__ == "__main__":
    start = "2017-05-16 00:00:00"
    end = "2017-05-16 00:01:00"

    def print_row(rows: list[dict]) -> None:
        for row in rows:
            print(row)
        print()

    ### COUNT LINES BY CATEGORY (INFO, WARNING, ERROR)
    # print_row(count_by_category(start, end))
    # print_row(count_by_category(start, end, category="INFO"))
    # print_row(count_by_category(start, end, category="WARNING"))
    # print_row(count_by_category(start, end, category="ERROR"))

    ### COUNT PID BY CATEGORY (INFO, WARNING, ERROR)
    # print_row(count_pid_by_category(start, end))
    # print_row(count_pid_by_category(start, end, category="INFO"))
    # print_row(count_pid_by_category(start, end, category="WARNING"))
    # print_row(count_pid_by_category(start, end, category="ERROR"))

    ### COUNT SERVICE BY CATEGORY (INFO, WARNING, ERROR)
    # print_row(count_service_by_category(start, end))
    # print_row(count_service_by_category(start, end, category="INFO"))
    # print_row(count_service_by_category(start, end, category="WARNING"))
    # print_row(count_service_by_category(start, end, category="ERROR"))

    ### COUNT EVENT ID BY CATEGORY (INFO, WARNING, ERROR)
    # print_row(count_event_id_by_category(start, end))
    # print_row(count_event_id_by_category(start, end, category="INFO"))
    # print_row(count_event_id_by_category(start, end, category="WARNING"))
    # print_row(count_event_id_by_category(start, end, category="ERROR"))
