from psycopg2 import sql

from backend.db.utils import (
    query_runner,
    TABLE_NAME,
)


@query_runner
def get_pids(table_name: str = TABLE_NAME) -> tuple[sql.Composable, list[str]]:
    print("Getting all pids")

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
    print("Getting all services")

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
    print("Getting all event_ids")

    query = sql.SQL("""
            SELECT DISTINCT event_id
            FROM {table_name}
            ORDER BY event_id
        """).format(
        table_name=sql.Identifier(table_name),
    )

    params = []
    return query, params


if __name__ == "__main__":

    def print_row(rows: list[dict]) -> None:
        for row in rows:
            print(row)
        print()

    print_row(get_pids())
    print_row(get_services())
    print_row(get_event_ids())
