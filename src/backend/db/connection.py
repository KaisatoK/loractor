"""SereneDB connection handling and the query-runner decorator.
Connections are served from a process-wide, thread-safe pool so concurrent
investigations share a bounded number of sockets, not one per query."""

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

from backend.db.capture import record_query
from backend.db.config import (
    SERENEDB_BULK_TIMEOUT_MS,
    SERENEDB_DB,
    SERENEDB_HOST,
    SERENEDB_INTERACTIVE_TIMEOUT_MS,
    SERENEDB_PASSWORD,
    SERENEDB_POOL_ACQUIRE_TIMEOUT,
    SERENEDB_POOL_MAX,
    SERENEDB_POOL_MIN,
    SERENEDB_PORT,
    SERENEDB_SESSION_MEMORY_LIMIT,
    SERENEDB_STATEMENT_TIMEOUT_MS,
    SERENEDB_USER,
)
from backend.db.errors import QueryRefused
from backend.db.stats import QueryClass, QueryResult, QueryStats

logger = logging.getLogger(__name__)
P = ParamSpec("P")

_pool: ThreadedConnectionPool | None = None
_pool_lock = threading.Lock()


def _connect_kwargs() -> dict:
    # NOTE: ``statement_timeout`` passed through libpq ``options`` is **silently
    # ignored** by SereneDB -- a connection opened with
    # ``-c statement_timeout=30000`` still reports ``0``. It is kept here because
    # it is harmless and correct against stock PostgreSQL, but it must not be
    # relied on: the timeout that actually binds is the ``SET LOCAL`` issued per
    # query class in :func:`query_runner`.
    #
    # ``memory_limit`` is deliberately NOT set here. It is a GLOBAL setting in
    # SereneDB ("option \"memory_limit\" cannot be set locally"), so putting it
    # in a connection string silently reconfigures the whole server for every
    # other client. Use :func:`set_global_memory_limit` as an explicit operator
    # action instead.
    return {
        "host": SERENEDB_HOST,
        "port": SERENEDB_PORT,
        "dbname": SERENEDB_DB,
        "user": SERENEDB_USER,
        "password": SERENEDB_PASSWORD,
        "application_name": "ai-chatbot",
        "options": f"-c statement_timeout={SERENEDB_STATEMENT_TIMEOUT_MS}",
    }


def current_memory_limit() -> str:
    """The server's global ``memory_limit``, as it reports it."""
    with db_cursor() as cur:
        cur.execute("SELECT current_setting('memory_limit') AS m")
        return str(cur.fetchone()["m"])


def set_global_memory_limit(limit: str = SERENEDB_SESSION_MEMORY_LIMIT) -> str:
    """Lower the server's global memory ceiling. **Explicit operator action.**

    Three measured facts make this a named function rather than a connection
    setting:

    * ``memory_limit`` cannot be scoped. ``SET LOCAL memory_limit`` fails with
      *option "memory_limit" cannot be set locally*, so there is no per-session
      or per-transaction form.
    * A plain ``SET`` is **global and persistent** -- it changes the ceiling for
      every other client of the server, and the new value survives the
      connection that set it.
    * Passing it through libpq ``options`` is unreliable; an explicit ``SET``
      statement is what actually applies.

    Worth doing on a host where index builds have proved fragile, because a
    ceiling the planner respects turns an out-of-memory crash into a failed
    query. But it reconfigures a shared server, so it is the operator's call --
    which is exactly why it is not a side effect of opening a connection.

    Returns the value the server reports afterwards, which may be rounded.
    """
    with db_cursor() as cur:
        cur.execute(
            sql.SQL("SET memory_limit = {limit}").format(limit=sql.Literal(limit))
        )
    return current_memory_limit()


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


def warm_pool() -> None:
    """Build the shared pool now instead of on the first query."""
    _get_pool()


def close_pool() -> None:
    """Close every pooled connection (a later query rebuilds the pool)."""
    global _pool
    with _pool_lock:
        pool, _pool = _pool, None
    if pool is not None:
        pool.closeall()
        logger.info("Closed the SereneDB connection pool")


def get_conn():
    """Open a standalone connection.
    Used by one-off tooling (schema setup, migrations), not the request path,
    which borrows from the shared pool via ``db_cursor``."""
    return psycopg2.connect(**_connect_kwargs())


# Every session reads and writes timestamps in UTC.
#
# The local corpora store TIMESTAMPTZ, and a naive window bound such as
# '2017-05-16 00:00:00' is interpreted in the *session* time zone when it is cast
# to timestamptz. Left to the server default (the TZ of whatever host runs it),
# the same question would select a different window on a different machine.
#
# Measured on SereneDB: ``SET TimeZone`` is session-scoped -- unlike
# ``memory_limit`` it does not reach other clients, and a fresh connection still
# reads the server default -- so it is safe to issue on every connection.
SESSION_TIME_ZONE = "UTC"


def set_session_time_zone(cur) -> None:
    """Pin ``cur``'s session to :data:`SESSION_TIME_ZONE`."""
    cur.execute(
        sql.SQL("SET TimeZone = {tz}").format(tz=sql.Literal(SESSION_TIME_ZONE))
    )


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
        set_session_time_zone(cur)
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
    func: Callable[P, tuple] | None = None,
    *,
    query_class: QueryClass = QueryClass.INTERACTIVE,
) -> Callable:
    """Turn a function that returns ``(query, params)`` into one that runs it.

    Three things happen around the execution that the builders do not have to
    think about:

    * **Per-class timeout.** ``SET LOCAL statement_timeout`` inside the
      transaction, so an interactive query and a bulk aggregate get different
      ceilings without needing different connections.
    * **Cost metering.** Wall clock is measured, and any
      :class:`~backend.db.stats.QueryStats` the builder produced from metadata
      pruning is attached to the result.
    * **Refusal handling.** A builder that raises
      :class:`~backend.db.errors.QueryRefused` never reaches SQL; the refusal
      comes back as a result so the agent can narrow and retry.

    A builder may return ``(query, params)`` or ``(query, params, stats)``.
    """

    def decorate(builder: Callable[P, tuple]) -> Callable[P, QueryResult]:
        @functools.wraps(builder)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> QueryResult:
            logger.debug(
                "Running %s args=%s kwargs=%s", builder.__name__, args, kwargs
            )
            try:
                built = builder(*args, **kwargs)
            except QueryRefused as refused:
                logger.info(
                    "Refused %s: %s", builder.__name__, refused.refusal.reason
                )
                return QueryResult(
                    [],
                    QueryStats(query_class=str(query_class)),
                    refusal=refused.refusal,
                )

            query, params, *rest = built
            stats: QueryStats = rest[0] if rest else QueryStats()
            stats.query_class = str(query_class)

            timeout_ms = (
                SERENEDB_BULK_TIMEOUT_MS
                if query_class is QueryClass.BULK
                else SERENEDB_INTERACTIVE_TIMEOUT_MS
            )

            started = time.monotonic()
            with db_cursor() as cur:
                cur.execute(
                    sql.SQL("SET LOCAL statement_timeout = {ms}").format(
                        ms=sql.Literal(timeout_ms)
                    )
                )
                cur.execute("SET sdb_ai_allow_insecure_endpoint = true")
                cur.execute(query, params)
                # The bound statement (``cursor.query``) is the executed query the
                # tool-result envelope reports; see ``backend.db.capture``.
                record_query(getattr(cur, "query", None))
                rows = [dict(row) for row in cur.fetchall()]

            stats.elapsed_ms = int((time.monotonic() - started) * 1000)
            return QueryResult(rows, stats)

        return wrapper

    if func is not None:
        return decorate(func)
    return decorate


def bulk_query(func: Callable[P, tuple]) -> Callable[P, QueryResult]:
    """``query_runner`` with the bulk timeout. For remote aggregates and builds."""
    return query_runner(func, query_class=QueryClass.BULK)
