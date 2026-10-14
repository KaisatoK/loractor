"""SereneDB connection handling and the query-runner decorator.

Connections are served from a process-wide, thread-safe pool so that many
concurrent investigations share a bounded number of sockets instead of opening
(and tearing down) one connection per query.
"""

import functools
import logging
import threading
import time
import psycopg2

from psycopg2 import sql
from psycopg2.extras import RealDictCursor
from psycopg2.pool import PoolError, ThreadedConnectionPool

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import ParamSpec

from backend.db.config import (
    SERENEDB_DB,
    SERENEDB_HOST,
    SERENEDB_PASSWORD,
    SERENEDB_POOL_ACQUIRE_TIMEOUT,
    SERENEDB_POOL_MAX,
    SERENEDB_POOL_MIN,
    SERENEDB_PORT,
    SERENEDB_STATEMENT_TIMEOUT_MS,
    SERENEDB_USER,
)

logger = logging.getLogger(__name__)
P = ParamSpec("P")

_pool: ThreadedConnectionPool | None = None
_pool_lock = threading.Lock()


def _connect_kwargs() -> dict:
    return {
        "host": SERENEDB_HOST,
        "port": SERENEDB_PORT,
        "dbname": SERENEDB_DB,
        "user": SERENEDB_USER,
        "password": SERENEDB_PASSWORD,
        "application_name": "ai-chatbot",
        # Enforce a server-side ceiling on how long any single query may run.
        "options": f"-c statement_timeout={SERENEDB_STATEMENT_TIMEOUT_MS}",
    }


def _get_pool() -> ThreadedConnectionPool:
    """Lazily build the shared pool (avoids connecting at import time)."""
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool = ThreadedConnectionPool(
                    SERENEDB_POOL_MIN,
                    SERENEDB_POOL_MAX,
                    **_connect_kwargs(),
                )
                logger.info(
                    "Created SereneDB connection pool (min=%s, max=%s)",
                    SERENEDB_POOL_MIN,
                    SERENEDB_POOL_MAX,
                )
    return _pool


def get_conn():
    """Open a standalone connection.

    Used by one-off tooling (schema setup, migrations) rather than the request
    path, which borrows from the shared pool via ``db_cursor``.
    """
    return psycopg2.connect(**_connect_kwargs())


@contextmanager
def db_cursor() -> Iterator[RealDictCursor]:
    pool = _get_pool()
    deadline = time.monotonic() + SERENEDB_POOL_ACQUIRE_TIMEOUT
    conn = None

    while conn is None:
        try:
            conn = pool.getconn()
        except PoolError:
            if time.monotonic() >= deadline:
                logger.error(
                    "Timed out after %.1fs waiting for a SereneDB connection "
                    "from the pool (max=%s).",
                    SERENEDB_POOL_ACQUIRE_TIMEOUT,
                    SERENEDB_POOL_MAX,
                )
                raise

            time.sleep(0.05)

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

        pool.putconn(conn)


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
