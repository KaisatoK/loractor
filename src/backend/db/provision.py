"""Bring a fresh SereneDB instance to the state the chatbot expects.

``bootstrap.sh`` owns the host side (uv, dependencies, starting a server);
this module owns everything that has to be said *to* the database, because
each step needs the project's own configuration to be said correctly:

* **Wait for the server.** A freshly started container or ``serened`` answers
  the port before it answers a query, so readiness is a successful ``SELECT``,
  not an open socket.
* **Create the embedding secret.** ``ai_embed`` runs inside SereneDB and
  resolves its endpoint and key from a server-side secret, so no amount of
  host-side configuration can replace it. It is created ``PERSISTENT`` here:
  a temporary secret lives only for the lifespan of the instance, which is
  what made every recreated container fail with ``secret 'gemini' not found``.
* **Lower the memory ceiling.** ``memory_limit`` is global, is not scoped to a
  session, and reverts to the server default on restart -- measured at 5.7 GiB
  against this project's 512 MiB budget, which is the configuration that
  OOM-killed index builds.
* **Report the state.** What is loaded, which secret exists, what the model
  really returns: the facts a setup script should print rather than assume.

Every step is idempotent, so running it again after a restart is the intended
way to recover an instance. The API key is passed in and composed as a SQL
literal; it is never logged, printed or returned.
"""

import argparse
import json
import logging
import os
import time

import psycopg2

from psycopg2 import sql
from psycopg2.extras import RealDictCursor

from backend.db import datasets
from backend.db.config import (
    EMBEDDING_DIMENSION,
    EMBEDDING_MODEL,
    EMBEDDING_SECRET,
    SERENEDB_DB,
    SERENEDB_HOST,
    SERENEDB_PASSWORD,
    SERENEDB_PORT,
    SERENEDB_SESSION_MEMORY_LIMIT,
    SERENEDB_USER,
)
from backend.db.connection import current_memory_limit, set_global_memory_limit
from backend.db.probe import probe_cursor
from backend.db.templates import (
    EMBEDDING_META_TABLE,
    stored_vector_width,
    template_table,
)

logger = logging.getLogger(__name__)

#: Google's OpenAI-compatible embeddings endpoint. The secret carries the
#: endpoint, so this -- not ``MODEL_BASE_URL`` -- is what decides where
#: ``ai_embed`` sends its requests.
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai"
EMBEDDINGS_PATH = "/embeddings"

#: The environment variable the embedding key is read from. It is the same key
#: the pipeline's Gemini chat model uses.
API_KEY_ENV = "GOOGLE_API_KEY"

WAIT_TIMEOUT_S = 120.0
WAIT_INTERVAL_S = 1.0


# --- waiting for the server ------------------------------------------------


def _probe_connection_kwargs() -> dict:
    """Connection arguments for a readiness probe.

    Deliberately not :func:`backend.db.connection._connect_kwargs`: that one is
    for the pool, while a readiness probe needs its own short ``connect_timeout``
    so a server that is not listening yet fails fast enough to retry.
    """
    return {
        "host": SERENEDB_HOST,
        "port": SERENEDB_PORT,
        "dbname": SERENEDB_DB,
        "user": SERENEDB_USER,
        "password": SERENEDB_PASSWORD,
        "application_name": "ai-chatbot-provision",
        "connect_timeout": 3,
    }


def server_is_up() -> bool:
    """Whether the server accepts a connection **and answers a query**.

    A listening socket is not readiness: a starting instance accepts the
    connection and then fails the first statement, so the probe runs one.
    """
    try:
        conn = psycopg2.connect(**_probe_connection_kwargs())
    except psycopg2.Error:
        return False
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT 1 AS ok")
            return cur.fetchone()["ok"] == 1
    except psycopg2.Error:
        return False
    finally:
        conn.close()


def wait_for_server(
    timeout_s: float = WAIT_TIMEOUT_S,
    interval_s: float = WAIT_INTERVAL_S,
    sleep=time.sleep,
    now=time.monotonic,
) -> float:
    """Block until the server answers, returning how long that took.

    Raises :class:`TimeoutError` with the target in the message, because the
    usual cause is a stale ``SERENEDB_PORT`` rather than a slow start -- the
    container's host port moves when it is recreated.
    """
    started = now()
    while True:
        if server_is_up():
            return now() - started
        waited = now() - started
        if waited + interval_s > timeout_s:
            raise TimeoutError(
                f"SereneDB did not answer on {SERENEDB_HOST}:{SERENEDB_PORT} "
                f"within {timeout_s:.0f}s. Check that the server is running and "
                f"that SERENEDB_PORT matches its published port."
            )
        sleep(interval_s)


# --- the embedding secret --------------------------------------------------


def secret_state(cur, name: str = EMBEDDING_SECRET) -> dict | None:
    """The named secret as the server reports it, or ``None``.

    ``sdb_secrets()`` redacts the credential, so the result is safe to print;
    ``persistent`` is the field that says whether it will survive a restart.
    """
    cur.execute(
        sql.SQL(
            "SELECT name, type, provider, persistent FROM sdb_secrets() "
            "WHERE name = {name}"
        ).format(name=sql.Literal(name))
    )
    row = cur.fetchone()
    return dict(row) if row else None


def ensure_embedding_secret(
    api_key: str,
    name: str = EMBEDDING_SECRET,
    base_url: str = GEMINI_BASE_URL,
    embeddings_path: str = EMBEDDINGS_PATH,
    persistent: bool = True,
    cur=None,
) -> dict:
    """Create (or replace) the secret ``ai_embed`` resolves its provider from.

    ``CREATE OR REPLACE`` rather than a create-if-absent check, so a rotated
    key or a secret left over as temporary is corrected rather than kept. The
    key is composed as a SQL literal and never logged: the caller gets back
    only what ``sdb_secrets()`` reports, which is redacted.

    **The other storage backend is dropped first**, and that is not tidiness.
    Temporary and persistent secrets live in separate backends (``memory`` and
    ``local_file`` in ``sdb_secrets()``), and ``CREATE OR REPLACE PERSISTENT``
    only replaces within the persistent one. Creating a persistent secret while
    a temporary one of the same name existed left both, and every ``ai_embed``
    call then failed with *"Ambiguity detected for secret name 'gemini', secret
    occurs in multiple storage backends"* -- a working setup turned into a
    broken one by adding the correct secret.
    """
    if not api_key:
        raise ValueError(
            f"no embedding API key: set {API_KEY_ENV} in .env or pass --api-key"
        )
    persistence = sql.SQL("PERSISTENT ") if persistent else sql.SQL("")
    # Drop the copy in the backend we are NOT about to write, so the name
    # resolves to exactly one secret.
    shadowed = sql.SQL("TEMPORARY") if persistent else sql.SQL("PERSISTENT")
    drop_shadowed = sql.SQL("DROP {kind} SECRET IF EXISTS {name}").format(
        kind=shadowed, name=sql.Identifier(name)
    )
    statement = sql.SQL(
        "CREATE OR REPLACE {persistence}SECRET {name} ("
        "    TYPE openai,"
        "    base_url {base_url},"
        "    embeddings_path {embeddings_path},"
        "    api_key {api_key}"
        ")"
    ).format(
        persistence=persistence,
        name=sql.Identifier(name),
        base_url=sql.Literal(base_url),
        embeddings_path=sql.Literal(embeddings_path),
        api_key=sql.Literal(api_key),
    )

    def _run(cursor) -> dict:
        cursor.execute(drop_shadowed)
        cursor.execute(statement)
        state = secret_state(cursor, name) or {"name": name, "persistent": persistent}
        logger.info(
            "embedding secret %r ready (type=%s persistent=%s) for %s",
            name,
            state.get("type"),
            state.get("persistent"),
            base_url,
        )
        return state

    if cur is not None:
        return _run(cur)
    with probe_cursor() as cursor:
        return _run(cursor)


def embedding_probe(cur=None) -> dict:
    """What the configured model really returns, measured in one call.

    One embedding call is the only way to learn the model's true width: the
    declared ``EMBEDDING_DIMENSION`` is what the table casts to, so a wider
    model is silently truncated rather than refused.
    """

    def _run(cursor) -> dict:
        cursor.execute("SET sdb_ai_allow_insecure_endpoint = true")
        try:
            cursor.execute(
                sql.SQL(
                    "SELECT len(ai_embed({text}, {model}, {secret})) AS dimension"
                ).format(
                    text=sql.Literal("a log line"),
                    model=sql.Literal(EMBEDDING_MODEL),
                    secret=sql.Literal(EMBEDDING_SECRET),
                )
            )
            measured = int(cursor.fetchone()["dimension"])
        except psycopg2.Error as exc:
            return {
                "can_embed": False,
                "error": str(exc).strip().splitlines()[0],
                "configured_dimension": EMBEDDING_DIMENSION,
            }
        return {
            "can_embed": True,
            "model_dimension": measured,
            "configured_dimension": EMBEDDING_DIMENSION,
            "dimension_matches_config": measured == EMBEDDING_DIMENSION,
        }

    if cur is not None:
        return _run(cur)
    with probe_cursor() as cursor:
        return _run(cursor)


# --- the memory ceiling ----------------------------------------------------


def apply_memory_ceiling(limit: str = SERENEDB_SESSION_MEMORY_LIMIT) -> dict:
    """Hold the server at the budgeted ceiling, reporting before and after.

    Separate from :func:`backend.db.external.hold_memory_ceiling`, which is a
    build's own guard: this is the operator action a setup run takes once, so a
    first index build does not meet the server default.
    """
    before = current_memory_limit()
    after = set_global_memory_limit(limit)
    logger.info("memory_limit %s -> %s (budget %s)", before, after, limit)
    return {"before": before, "after": after, "requested": limit}


# --- what is loaded --------------------------------------------------------


def _table_exists(cur, table: str) -> bool:
    cur.execute(
        sql.SQL(
            "SELECT count(*) AS n FROM information_schema.tables "
            "WHERE table_name = {t}"
        ).format(t=sql.Literal(table))
    )
    return int(cur.fetchone()["n"]) > 0


def corpus_state(cur=None) -> dict:
    """Row counts for every local corpus, plus its template table.

    Reported per registered dataset rather than per known table name, so a
    corpus added to the registry appears here without touching this module.
    """

    def _run(cursor) -> dict:
        state: dict[str, dict] = {}
        for dataset in datasets.local_datasets():
            table = dataset.sources[0]
            entry: dict = {"table": table, "loaded": False, "rows": 0}
            if _table_exists(cursor, table):
                cursor.execute(
                    sql.SQL("SELECT count(*) AS n FROM {t}").format(
                        t=sql.Identifier(table)
                    )
                )
                entry["loaded"] = True
                entry["rows"] = int(cursor.fetchone()["n"])
                entry["expected_rows"] = dataset.row_estimate
            templates = template_table(dataset)
            if _table_exists(cursor, templates):
                cursor.execute(
                    sql.SQL(
                        "SELECT count(*) AS n, count(embedding) AS embedded FROM {t}"
                    ).format(t=sql.Identifier(templates))
                )
                row = cursor.fetchone()
                entry["templates"] = int(row["n"])
                entry["embedded"] = int(row["embedded"])
                entry["stored_dimension"] = stored_vector_width(cursor, templates)
            state[dataset.id] = entry
        state["team_ownership"] = {
            "table": "team_ownership",
            "loaded": _table_exists(cursor, "team_ownership"),
        }
        if state["team_ownership"]["loaded"]:
            cursor.execute("SELECT count(*) AS n FROM team_ownership")
            state["team_ownership"]["rows"] = int(cursor.fetchone()["n"])
        state["embedding_meta"] = {
            "table": EMBEDDING_META_TABLE,
            "loaded": _table_exists(cursor, EMBEDDING_META_TABLE),
        }
        return state

    if cur is not None:
        return _run(cur)
    with probe_cursor() as cursor:
        return _run(cursor)


def pending_datasets(requested: list[str], strict_rows: bool = False) -> list[str]:
    """Which of ``requested`` still need loading.

    A load **drops and rebuilds** its table, and BGL's 4.7M rows take a long
    time, so a second ``bootstrap.sh`` run must not redo one that is already
    there. A corpus counts as loaded when its table exists and is not empty;
    ``strict_rows`` additionally requires the registry's expected row count,
    which the 2,000-row loghub samples deliberately do not meet.

    Loader ids ending in ``_2k`` load the sample into the same table as the
    full corpus, so they are judged by that table.
    """
    state = corpus_state()
    still: list[str] = []
    for name in requested:
        dataset_id = name[:-3] if name.endswith("_2k") else name
        entry = state.get(dataset_id)
        if entry is None or not entry.get("loaded") or entry.get("rows", 0) == 0:
            still.append(name)
            continue
        expected = entry.get("expected_rows")
        if strict_rows and expected and entry["rows"] != expected:
            still.append(name)
    return still


def report(probe_embedding: bool = False) -> dict:
    """Everything a setup run should print: server, secret, ceiling, corpora.

    ``probe_embedding`` costs one provider request, so it is opt-in.
    """
    with probe_cursor() as cur:
        cur.execute("SELECT current_setting('memory_limit') AS m")
        memory_limit = str(cur.fetchone()["m"])
        state = {
            "server": f"{SERENEDB_HOST}:{SERENEDB_PORT}/{SERENEDB_DB}",
            "memory_limit": memory_limit,
            "memory_budget": SERENEDB_SESSION_MEMORY_LIMIT,
            "secret": secret_state(cur),
            "secret_name": EMBEDDING_SECRET,
            "embedding_model": EMBEDDING_MODEL,
            "configured_dimension": EMBEDDING_DIMENSION,
            "corpora": corpus_state(cur),
        }
        if probe_embedding:
            state["embedding"] = embedding_probe(cur)
    return state


def format_report(state: dict) -> str:
    """The report as the lines a setup script prints."""
    lines = [f"SereneDB      {state['server']}"]
    lines.append(
        f"memory_limit  {state['memory_limit']} (budget {state['memory_budget']})"
    )
    secret = state["secret"]
    if secret:
        survives = "survives a restart" if secret["persistent"] else "TEMPORARY"
        lines.append(
            f"secret        {secret['name']} (type {secret['type']}, {survives})"
        )
    else:
        lines.append(
            f"secret        {state['secret_name']} MISSING -- ai_embed cannot run"
        )
    embedding = state.get("embedding")
    if embedding and embedding.get("can_embed"):
        agree = "matches" if embedding["dimension_matches_config"] else "DISAGREES with"
        lines.append(
            f"embedding     {state['embedding_model']} returns "
            f"{embedding['model_dimension']} dimensions, {agree} the configured "
            f"{embedding['configured_dimension']}"
        )
    elif embedding:
        lines.append(f"embedding     cannot embed: {embedding['error']}")
    for name, entry in state["corpora"].items():
        if name in ("team_ownership", "embedding_meta"):
            continue
        if not entry["loaded"]:
            lines.append(f"corpus        {name:10} not loaded")
            continue
        detail = f"{entry['rows']:,} rows"
        expected = entry.get("expected_rows")
        if expected and entry["rows"] != expected:
            detail += f" (registry expects {expected:,})"
        if "templates" in entry:
            detail += (
                f", {entry['templates']:,} templates, {entry['embedded']:,} embedded"
            )
            width = entry.get("stored_dimension")
            if width:
                detail += f" at {width} dimensions"
        lines.append(f"corpus        {name:10} {detail}")
    ownership = state["corpora"]["team_ownership"]
    lines.append(
        f"ownership     {ownership.get('rows', 0):,} rows"
        if ownership["loaded"]
        else "ownership     not loaded"
    )
    return "\n".join(lines)


# --- CLI -------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m backend.db.provision",
        description=(
            "Prepare a SereneDB instance for the chatbot: wait for it, create "
            "the embedding secret, hold the memory ceiling, report the state."
        ),
    )
    parser.add_argument(
        "--wait",
        nargs="?",
        type=float,
        const=WAIT_TIMEOUT_S,
        default=None,
        metavar="SECONDS",
        help=f"wait for the server to answer (default {WAIT_TIMEOUT_S:.0f}s)",
    )
    parser.add_argument(
        "--secret",
        action="store_true",
        help=(
            f"create or replace the {EMBEDDING_SECRET!r} embedding secret from "
            f"${API_KEY_ENV}"
        ),
    )
    parser.add_argument(
        "--api-key",
        default=None,
        help=f"the embedding key (default: ${API_KEY_ENV}); prefer the env var",
    )
    parser.add_argument(
        "--temporary-secret",
        action="store_true",
        help="create the secret for this instance only, not PERSISTENT",
    )
    parser.add_argument(
        "--memory",
        action="store_true",
        help="lower the server's global memory_limit to the budget",
    )
    parser.add_argument(
        "--probe-embedding",
        action="store_true",
        help="measure the model's real width (costs one provider request)",
    )
    parser.add_argument(
        "--pending-datasets",
        nargs="*",
        default=None,
        metavar="DATASET",
        help=(
            "print, one per line, which of the named loader datasets still need "
            "loading; a loaded corpus is left alone because a load rebuilds it"
        ),
    )
    parser.add_argument(
        "--strict-rows",
        action="store_true",
        help="with --pending-datasets, also reload a corpus whose row count "
        "differs from the registry's estimate",
    )
    parser.add_argument(
        "--report", action="store_true", help="print the resulting state"
    )
    parser.add_argument("--json", action="store_true", help="print the state as JSON")
    args = parser.parse_args(argv)

    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))

    if args.wait is not None:
        waited = wait_for_server(args.wait)
        print(f"SereneDB answered after {waited:.1f}s")

    if args.memory:
        moved = apply_memory_ceiling()
        print(f"memory_limit {moved['before']} -> {moved['after']}")

    if args.secret:
        key = args.api_key or os.getenv(API_KEY_ENV, "")
        state = ensure_embedding_secret(key, persistent=not args.temporary_secret)
        kind = "persistent" if state.get("persistent") else "temporary"
        print(f"embedding secret {state['name']!r} ready ({kind})")

    if args.pending_datasets is not None:
        for name in pending_datasets(args.pending_datasets, args.strict_rows):
            print(name)

    if args.report or args.json:
        state = report(probe_embedding=args.probe_embedding)
        print(json.dumps(state, indent=2) if args.json else format_report(state))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
