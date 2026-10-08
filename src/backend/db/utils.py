import functools
import logging
from collections.abc import Callable
from contextlib import contextmanager
from typing import ParamSpec

import psycopg2
from psycopg2 import sql
from psycopg2.extras import RealDictCursor

SERENEDB_PORT = 7890
MODEL_BASE_URL = "http://host.docker.internal:11434"
EMBEDDING_MODEL = "nomic-embed-text:v1.5"
EMBEDDING_DIMENSION = 768
TABLE_NAME = "log"

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
    @functools.wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> list[dict]:
        logger.info("Running %s args=%s kwargs=%s", func.__name__, args, kwargs)

        query, params = func(*args, **kwargs)

        with db_cursor() as cur:
            cur.execute("SET sdb_ai_allow_insecure_endpoint = true")
            cur.execute(query, params)
            return [dict(row) for row in cur.fetchall()]

    return wrapper
