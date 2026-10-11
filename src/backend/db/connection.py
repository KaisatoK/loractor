"""SereneDB connection handling and the query-runner decorator."""

import functools
import logging
import psycopg2

from psycopg2 import sql
from psycopg2.extras import RealDictCursor
from collections.abc import Callable
from contextlib import contextmanager
from typing import Generator, ParamSpec
from backend.db.config import SERENEDB_PORT

logger = logging.getLogger(__name__)
P = ParamSpec("P")


def get_conn():
    return psycopg2.connect(
        f"host=localhost port={SERENEDB_PORT} dbname=postgres user=postgres password=postgres"
    )


@contextmanager
def db_cursor() -> Generator[str, None, None]:
    conn = get_conn()
    cur = None

    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        yield cur
        conn.commit()
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
    """Turn a function that returns ``(query, params)`` into one that runs it."""

    @functools.wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> list[dict]:
        logger.debug("Running %s args=%s kwargs=%s", func.__name__, args, kwargs)

        query, params = func(*args, **kwargs)

        with db_cursor() as cur:
            cur.execute("SET sdb_ai_allow_insecure_endpoint = true")
            cur.execute(query, params)
            return [dict(row) for row in cur.fetchall()]

    return wrapper
