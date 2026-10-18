"""Template-level embeddings: semantic search without embedding every row.

Embedding a billion log lines is not affordable, and it is not necessary.
Machine-generated logs are highly repetitive: the same handful of code paths emit
the same handful of message shapes, differing only in ids, numbers and
timestamps. Collapsing those variables to ``<*>`` leaves a small set of
**templates**, and the templates are what carry meaning.

Measured on the built OTel slice: **150,557 rows reduce to 768 distinct
templates**, a 196x reduction. Embedding 768 texts takes ~64 s at ~0.083 s each;
embedding 150,557 would take ~3.5 hours, and the billion-row corpus is out of
the question entirely. The reduction is what makes semantic search possible here
at all.

The trade is honest and worth stating: a template embedding describes a *kind* of
event, not an individual line. A query matches the shape of a message, then every
row of that shape is retrieved. That is usually what an investigator wants
("show me card-charge failures"), but it cannot distinguish two rows sharing a
template -- so the lexical branch, which does see the full text of each row,
stays in the fusion to provide exactly that discrimination.

OpenStack keeps its per-row embeddings untouched. Its corpus is 2,000 rows, where
per-row embedding is cheap and strictly better.
"""

from __future__ import annotations

import logging
import os
import time

from psycopg2 import sql

from backend.db.config import (
    EMBEDDING_DIMENSION,
    EMBEDDING_MODEL,
    EMBEDDING_SECRET,
)
from backend.db.datasets import Capability, Dataset, resolve
from backend.db.errors import refuse

logger = logging.getLogger(__name__)

# Templates embedded per statement. Each row makes its own HTTP call to the
# embedding service from inside the server, so a batch bounds how much work one
# statement holds open rather than how much memory it needs.
EMBED_BATCH = 200

# Most templates one build will embed.
#
# Guards against a corpus whose templating failed to collapse anything: at
# ~0.083 s per call, 20,352 templates (full BGL) take ~28 minutes, so a corpus
# with 500,000 would run for half a day unnoticed. Refusing is better than
# discovering that at hour six.
#
# Deliberately not a "top N by frequency" truncation. In BGL the 100 commonest
# templates cover 93.6% of rows, so a frequency cut would look almost free --
# but it would drop the rare templates, and in log investigation the rare event
# is the signal. A one-off FATAL matters more than the millionth routine line.
MAX_TEMPLATES = int(os.getenv("SERENEDB_MAX_TEMPLATES", "50000"))

# Templates nearer than this are considered candidate matches. Cosine distance,
# so 0 is identical and 2 is opposite. Deliberately generous: the fusion ranks
# the results, this only decides what reaches the ranking.
MAX_TEMPLATE_DISTANCE = 1.0

# Templates retrieved for one query before expansion to rows.
TEMPLATE_BRANCH_WIDTH = 20


def template_table(dataset: Dataset | str) -> str:
    """Name of a dataset's template side table."""
    ds = resolve(dataset) if isinstance(dataset, str) else dataset
    return f"{ds.id}_templates"


def _require_templates(dataset: Dataset | str) -> Dataset:
    """Resolve a dataset that can actually have template embeddings."""
    ds = resolve(dataset) if isinstance(dataset, str) else dataset
    if not ds.supports(Capability.TEMPLATES):
        raise refuse(
            f"Dataset '{ds.id}' has no parsed event templates.",
            "Template embeddings need a template field; use a dataset that "
            "declares the templates capability.",
            code="unsupported_capability",
            dataset=ds.id,
        )
    return ds


def _embed_cursor():
    """Autocommit cursor with the AI endpoint allowed.

    ``ai_embed`` refuses a plain-HTTP endpoint unless
    ``sdb_ai_allow_insecure_endpoint`` is set; the query path sets it per query,
    so the build path has to as well.
    """
    from backend.db.probe import probe_cursor

    return probe_cursor()


# One row per dataset: which model produced the stored vectors, and how wide
# they are. Without it a model change is invisible -- the vectors look fine and
# the distances are meaningless.
EMBEDDING_META_TABLE = "template_embedding_meta"

# Text the probe embeds to measure a model's real output width. Short on
# purpose: it costs one call.
_PROBE_TEXT = "a log line"


def _ensure_meta(cur) -> None:
    cur.execute(
        sql.SQL("""
            CREATE TABLE IF NOT EXISTS {t} (
                dataset VARCHAR,
                model VARCHAR,
                dimension BIGINT,
                built_at VARCHAR
            )
        """).format(t=sql.Identifier(EMBEDDING_META_TABLE))
    )


def _record_embedding_model(cur, dataset_id: str) -> None:
    """Record the model and width the stored vectors were built with."""
    _ensure_meta(cur)
    cur.execute(
        sql.SQL("DELETE FROM {t} WHERE dataset = %s").format(
            t=sql.Identifier(EMBEDDING_META_TABLE)
        ),
        (dataset_id,),
    )
    cur.execute(
        sql.SQL("INSERT INTO {t} (dataset, model, dimension, built_at) "
                "VALUES (%s, %s, %s, %s)").format(
            t=sql.Identifier(EMBEDDING_META_TABLE)
        ),
        (
            dataset_id,
            EMBEDDING_MODEL,
            EMBEDDING_DIMENSION,
            time.strftime("%Y-%m-%d %H:%M:%S+00", time.gmtime()),
        ),
    )


def stored_embedding_model(cur, dataset_id: str) -> dict | None:
    """The model the stored vectors were built with, or ``None`` if unrecorded.

    ``None`` is the honest answer for a table built before the model was
    recorded: the vectors may have come from any model, so nothing can be
    concluded from them being present.
    """
    try:
        cur.execute(
            sql.SQL("SELECT model, dimension, built_at FROM {t} "
                    "WHERE dataset = %s LIMIT 1").format(
                t=sql.Identifier(EMBEDDING_META_TABLE)
            ),
            (dataset_id,),
        )
        row = cur.fetchone()
    except Exception:  # noqa: BLE001 -- no record is an answer, not an error
        return None
    return dict(row) if row else None


def probe_embedding_dimension(cur) -> int:
    """Embed one short string and return how many dimensions came back.

    This is the only way to know what the configured model really produces:
    ``ai_embed`` resolves its endpoint from the server-side secret, so the
    model name in Python says nothing about what the database will return.
    """
    cur.execute("SET sdb_ai_allow_insecure_endpoint = true")
    cur.execute(
        sql.SQL("SELECT ai_embed(%s, {model}, {secret}) AS emb").format(
            model=sql.Literal(EMBEDDING_MODEL),
            secret=sql.Literal(EMBEDDING_SECRET),
        ),
        (_PROBE_TEXT,),
    )
    return len(cur.fetchone()["emb"])


def stored_vector_width(cur, table: str) -> int | None:
    """The width of a stored vector, measured from one row.

    The column is declared ``FLOAT[n]`` but SereneDB reports its type only as
    ``ARRAY``, so the width cannot be read from the catalog. One row settles
    it, and it also catches a model change that was never recorded: a
    different width is a different model.
    """
    try:
        cur.execute(
            sql.SQL("SELECT embedding FROM {t} WHERE embedding IS NOT NULL "
                    "LIMIT 1").format(t=sql.Identifier(table))
        )
        row = cur.fetchone()
    except Exception:  # noqa: BLE001 -- no table or no vectors is an answer
        return None
    return len(row["embedding"]) if row and row["embedding"] else None


# Problems a rebuild resolves by dropping the table, as opposed to ones that
# would break the rebuild too.
STALE_CODES = frozenset({"stale_model", "stale_dimension"})


def embedding_compatibility(dataset: Dataset | str) -> dict:
    """Whether the configured embedding model can be used for this dataset.

    Checks the three ways a model change breaks template embeddings, each of
    which is otherwise silent or obscure:

    * the database cannot embed at all -- usually the server-side secret is
      missing, which it is after the container is recreated;
    * the model returns a different width than the table stores, which the
      cast would truncate rather than reject;
    * the stored vectors came from a different model, which makes every
      distance against a new query vector meaningless.
    """
    ds = resolve(dataset) if isinstance(dataset, str) else dataset
    table = template_table(ds)
    report: dict = {
        "dataset": ds.id,
        "model": EMBEDDING_MODEL,
        "secret": EMBEDDING_SECRET,
        "declared_dimension": EMBEDDING_DIMENSION,
        "actual_dimension": None,
        "stored_dimension": None,
        "can_embed": False,
        "stored": None,
        "ok": False,
        "problems": [],
    }

    def problem(code: str, message: str) -> None:
        report["problems"].append({"code": code, "message": message})

    with _embed_cursor() as cur:
        try:
            report["actual_dimension"] = probe_embedding_dimension(cur)
            report["can_embed"] = True
        except Exception as error:  # noqa: BLE001 -- reported, not raised
            detail = str(error).strip().splitlines()[0]
            problem("cannot_embed",
                    f"the database cannot embed with '{EMBEDDING_MODEL}': {detail}")
            if "secret" in detail.lower():
                problem(
                    "missing_secret",
                    f"create the server-side secret the ai_embed calls name "
                    f"('{EMBEDDING_SECRET}') so it points at a provider "
                    f"serving this model; a recreated container loses its "
                    f"secrets",
                )
        report["stored"] = stored_embedding_model(cur, ds.id)
        report["stored_dimension"] = stored_vector_width(cur, table)

    actual = report["actual_dimension"]
    if actual is not None and actual != EMBEDDING_DIMENSION:
        problem(
            "declared_dimension",
            f"'{EMBEDDING_MODEL}' returns {actual} dimensions but the table "
            f"stores {EMBEDDING_DIMENSION}; set EMBEDDING_DIMENSION={actual} "
            "and rebuild, or ask the endpoint for a smaller output width",
        )

    stored = report["stored"]
    if stored and stored.get("model") != EMBEDDING_MODEL:
        problem(
            "stale_model",
            f"the stored vectors were built with '{stored['model']}' and the "
            f"configured model is '{EMBEDDING_MODEL}'; distances between the "
            "two are meaningless, so rebuild with rebuild=True",
        )

    width = report["stored_dimension"]
    if width is not None and width != EMBEDDING_DIMENSION:
        problem(
            "stale_dimension",
            f"the stored vectors are {width}-wide but {EMBEDDING_DIMENSION} is "
            "configured, so they came from another model and would be mixed "
            "with new ones; rebuild with rebuild=True",
        )

    report["ok"] = report["can_embed"] and not report["problems"]
    return report


def _unembedded(cur, table: str) -> int:
    """Templates still awaiting an embedding.

    The loop's termination condition depends on this falling, so it is measured
    from the table rather than inferred from rows-affected counts.
    """
    cur.execute(
        sql.SQL("""
            SELECT count(*) AS n FROM {t}
            WHERE embedding IS NULL AND template_text IS NOT NULL
        """).format(t=sql.Identifier(table))
    )
    return int(cur.fetchone()["n"])


def build_templates(
    dataset: Dataset | str, *, rebuild: bool = False, progress=None
) -> dict:
    """Extract distinct templates, embed them, and index the vectors.

    Costs one embedding call per *distinct template*, not per row. The row count
    is carried alongside so the agent can see how much of the corpus a matched
    template actually accounts for -- a template with 16,584 rows behind it is a
    different finding from one with 1.
    """
    ds = _require_templates(dataset)
    table = template_table(ds)
    template_sql = ds.sql_for("template")
    source = ds.scan_relation()

    report: dict = {"dataset": ds.id, "table": table}

    # Checked before anything is built, because each failure mode is otherwise
    # expensive or silent: a missing secret fails on the first batch, a width
    # mismatch is truncated by the cast, and filling only the NULL embeddings
    # with a different model leaves one table holding two models' vectors,
    # whose distances mean nothing. A rebuild drops the table, so mixing cannot
    # happen and only the live checks apply.
    compatibility = embedding_compatibility(ds)
    report["compatibility"] = compatibility
    blocking = [
        item for item in compatibility["problems"]
        if not (rebuild and item["code"] in STALE_CODES)
    ]
    if blocking:
        raise refuse(
            f"The configured embedding model cannot build {ds.id} templates.",
            " Also: ".join(item["message"] for item in blocking),
            code="embedding_incompatible",
            model=compatibility["model"],
            declared_dimension=compatibility["declared_dimension"],
            actual_dimension=compatibility["actual_dimension"],
            stored=compatibility["stored"],
        )

    with _embed_cursor() as cur:
        cur.execute("SET sdb_ai_allow_insecure_endpoint = true")

        if rebuild:
            cur.execute(
                sql.SQL("DROP TABLE IF EXISTS {t} CASCADE").format(
                    t=sql.Identifier(table)
                )
            )

        cur.execute(
            sql.SQL("""
                CREATE TABLE IF NOT EXISTS {t} (
                    template_id BIGINT,
                    template_text VARCHAR,
                    row_count BIGINT,
                    embedding FLOAT[{dim}]
                )
            """).format(t=sql.Identifier(table), dim=sql.Literal(EMBEDDING_DIMENSION))
        )

        # Distinct templates with their row counts, in one aggregate. No message
        # text is read beyond the template expression itself.
        #
        # Templates with no text are excluded: 34,470 BGL rows carry no message,
        # so their template is empty, and an embedding of nothing would be a
        # vector that matches everything equally. They stay queryable by every
        # other means -- only semantic search cannot reach them.
        started = time.monotonic()
        cur.execute(
            sql.SQL("""
                CREATE OR REPLACE TEMP VIEW _pending AS
                SELECT {template} AS template_text, count(*) AS row_count
                FROM {source}
                WHERE {template} IS NOT NULL AND {template} <> ''
                GROUP BY 1
            """).format(template=template_sql, source=source)
        )
        cur.execute("SELECT count(*) AS n FROM _pending")
        total = int(cur.fetchone()["n"])
        report["distinct_templates"] = total

        if total > MAX_TEMPLATES:
            raise refuse(
                f"{ds.id} has {total:,} distinct templates, above the "
                f"{MAX_TEMPLATES:,} cap.",
                "Templating is not collapsing variable parts; widen the "
                "template expression before embedding, or raise "
                "SERENEDB_MAX_TEMPLATES if the count is genuinely correct.",
                code="too_many_templates",
                dataset=ds.id,
                templates=total,
            )
        report["extract_seconds"] = round(time.monotonic() - started, 2)

        # Two phases, because combining them does not terminate reliably.
        #
        # The first version embedded and inserted in one statement, re-selecting
        # whatever was "not yet present" each round. That loops forever on a
        # NULL template, since `existing = pending` is never true for NULL, and
        # it silently re-embedded rows -- a run reported 38,371 of 20,353 done.
        #
        # Rows are now inserted without embeddings, then embeddings are filled
        # in where they are missing. Both phases are idempotent and an
        # interrupted build resumes where it stopped.
        started = time.monotonic()

        # Ids continue from the current maximum. `row_number()` alone restarts
        # at 1, so a resumed build would hand out ids that already exist and the
        # update below would then target the wrong rows.
        cur.execute(
            sql.SQL("SELECT COALESCE(max(template_id), 0) AS m FROM {t}").format(
                t=sql.Identifier(table)
            )
        )
        id_offset = int(cur.fetchone()["m"])

        cur.execute(
            sql.SQL("""
                INSERT INTO {t} (template_id, template_text, row_count)
                SELECT {offset} + row_number() OVER (),
                       p.template_text, p.row_count
                FROM _pending p
                WHERE NOT EXISTS (
                    SELECT 1 FROM {t} e WHERE e.template_text = p.template_text
                )
            """).format(t=sql.Identifier(table), offset=sql.Literal(id_offset))
        )
        report["rows_inserted"] = max(cur.rowcount, 0)

        embedded = 0
        report["incomplete_reason"] = None

        # Termination is enforced three ways rather than trusted:
        #
        # 1. an arithmetic ceiling on iterations, from the work outstanding;
        # 2. the count of unembedded rows must *strictly decrease* each round;
        # 3. the loop stops as soon as nothing is outstanding.
        #
        # The second is the one that matters. `UPDATE SET embedding =
        # ai_embed(...)` reports rows affected even when the function yields
        # NULL, so a single template the embedding service cannot handle would
        # otherwise be re-selected forever at a steady 200 rows per round,
        # looking like healthy progress. Counting what is left cannot be fooled
        # that way.
        remaining = _unembedded(cur, table)
        max_rounds = remaining // EMBED_BATCH + 2

        for _ in range(max_rounds):
            if not remaining:
                break

            cur.execute(
                sql.SQL("""
                    UPDATE {t} SET embedding =
                        ai_embed(template_text, {model}, {secret})
                            ::FLOAT[{dim}]
                    WHERE template_id IN (
                        SELECT template_id FROM {t}
                        WHERE embedding IS NULL AND template_text IS NOT NULL
                        ORDER BY template_id
                        LIMIT {batch}
                    )
                """).format(
                    t=sql.Identifier(table),
                    model=sql.Literal(EMBEDDING_MODEL),
                    secret=sql.Literal(EMBEDDING_SECRET),
                    dim=sql.Literal(EMBEDDING_DIMENSION),
                    batch=sql.Literal(EMBED_BATCH),
                )
            )

            still_left = _unembedded(cur, table)
            if still_left >= remaining:
                # No forward progress: the batch at the head of the queue cannot
                # be embedded. Stop and say so, rather than spin.
                report["incomplete_reason"] = (
                    f"embedding stopped making progress with {still_left:,} "
                    "templates left; the next batch could not be embedded"
                )
                logger.warning(
                    "Embedding stalled for %s at %d remaining", ds.id, still_left
                )
                break

            embedded += remaining - still_left
            remaining = still_left
            logger.info("Embedded %d/%d templates for %s", embedded, total, ds.id)
            if progress is not None:
                progress(embedded, total)
        else:
            if remaining:
                report["incomplete_reason"] = (
                    f"hit the {max_rounds}-round ceiling with {remaining:,} left"
                )

        report["embedded"] = embedded
        report["unembedded"] = remaining
        report["embed_seconds"] = round(time.monotonic() - started, 2)

        cur.execute(
            sql.SQL("VACUUM (REFRESH_TABLE) {t}").format(t=sql.Identifier(table))
        )
        cur.execute(
            sql.SQL("SELECT pg_total_relation_size({t}) AS b").format(
                t=sql.Literal(table)
            )
        )
        report["bytes"] = int(cur.fetchone()["b"])

        # Which model these vectors came from, so a later model change is
        # detectable instead of silently producing meaningless distances.
        _record_embedding_model(cur, ds.id)

    set_vector_cache(ds.id, bool(report.get("distinct_templates")))
    logger.info("Template embeddings built: %s", report)
    return report


def drop_templates(dataset: Dataset | str) -> dict:
    """Remove a dataset's template table."""
    ds = resolve(dataset) if isinstance(dataset, str) else dataset
    table = template_table(ds)
    with _embed_cursor() as cur:
        cur.execute(
            sql.SQL("DROP TABLE IF EXISTS {t} CASCADE").format(
                t=sql.Identifier(table)
            )
        )
    set_vector_cache(ds.id, False)
    return {"dropped": table}


def templates_status(dataset: Dataset | str) -> dict:
    """Whether a dataset's template embeddings exist and how complete they are.

    Completeness matters for honesty: embeddings covering 700 of 768 templates
    will silently miss the other 68, so a semantic search that finds nothing is
    not evidence that nothing is there.
    """
    ds = resolve(dataset) if isinstance(dataset, str) else dataset
    table = template_table(ds)
    with _embed_cursor() as cur:
        cur.execute(
            "SELECT count(*) AS n FROM information_schema.tables "
            "WHERE table_name = %s",
            (table,),
        )
        if not int(cur.fetchone()["n"]):
            return {"exists": False, "dataset": ds.id, "table": table}

        cur.execute(
            sql.SQL("""
                SELECT count(*) AS templates,
                       sum(row_count) AS rows_covered,
                       count(embedding) AS embedded
                FROM {t}
            """).format(t=sql.Identifier(table))
        )
        row = cur.fetchone()
        stored = stored_embedding_model(cur, ds.id)
        width = stored_vector_width(cur, table)

    return {
        "exists": True,
        "dataset": ds.id,
        "table": table,
        "templates": int(row["templates"]),
        "embedded": int(row["embedded"] or 0),
        "rows_covered": int(row["rows_covered"] or 0),
        "complete": int(row["embedded"] or 0) == int(row["templates"]),
        # What produced the vectors, and whether that is still what is
        # configured. ``None`` means the table predates model recording, so the
        # vectors cannot be attributed to any model.
        "model": (stored or {}).get("model"),
        "dimension": (stored or {}).get("dimension"),
        "model_matches_config": (
            None if stored is None else stored.get("model") == EMBEDDING_MODEL
        ),
        "configured_model": EMBEDDING_MODEL,
        # Measured from a stored row, so it is known even for a table built
        # before the model was recorded. A width that differs from the
        # configured one proves the vectors came from another model.
        "stored_dimension": width,
        "dimension_matches_config": (
            None if width is None else width == EMBEDDING_DIMENSION
        ),
        "configured_dimension": EMBEDDING_DIMENSION,
    }


# Whether a dataset's template vectors exist, cached per process.
#
# This is a build-time fact, not a per-query one: the table appears when an
# operator builds it and disappears when one drops it. Probing the database on
# every search would add a round trip to decide the shape of the search, and --
# worse -- would make the query layer's behaviour depend on live state, so the
# same call would compose different SQL on two machines. Cached, with an
# explicit seam for tests and for the build path to invalidate.
_VECTOR_CACHE: dict[str, bool] = {}


def clear_vector_cache() -> None:
    """Forget cached template-vector availability."""
    _VECTOR_CACHE.clear()


def set_vector_cache(dataset_id: str, available: bool) -> None:
    """Record template-vector availability without touching the database."""
    _VECTOR_CACHE[dataset_id] = available


def has_template_vectors(dataset: Dataset | str) -> bool:
    """Whether the template table exists and is usable for search.

    Absence is normal -- the table is an operator action -- so this never
    raises; a corpus without template vectors simply searches lexically.
    """
    ds = resolve(dataset) if isinstance(dataset, str) else dataset
    if ds.id in _VECTOR_CACHE:
        return _VECTOR_CACHE[ds.id]
    try:
        status = templates_status(ds)
        available = bool(status.get("exists") and status.get("embedded"))
        # Vectors from a different model than the one that will embed the query
        # are worse than no vectors: every distance is meaningless, and the
        # search would rank confidently on noise. Searching lexically instead
        # is the honest degradation.
        if available and status.get("dimension_matches_config") is False:
            # Measured, so this holds even when no model was recorded: a
            # 768-wide vector cannot be compared with a 3072-wide query one.
            logger.warning(
                "%s template vectors are %s-wide but %s is configured; "
                "searching lexically until they are rebuilt",
                ds.id, status.get("stored_dimension"),
                status.get("configured_dimension"),
            )
            available = False
        elif available and status.get("model_matches_config") is False:
            logger.warning(
                "%s template vectors were built with '%s' but '%s' is "
                "configured; searching lexically until they are rebuilt",
                ds.id, status.get("model"), status.get("configured_model"),
            )
            available = False
        elif available and status.get("model_matches_config") is None:
            # Built before the model was recorded, so it cannot be attributed.
            logger.info(
                "%s template vectors predate model recording; rebuild to "
                "confirm they match '%s'", ds.id, status.get("configured_model"),
            )
    except Exception:  # noqa: BLE001 - absence must never break search
        logger.debug("Template status unavailable for %s", ds.id, exc_info=True)
        available = False
    _VECTOR_CACHE[ds.id] = available
    return available


def template_branch(
    dataset: Dataset,
    *,
    join: sql.Composable | None = None,
    where: sql.Composable | None = None,
) -> tuple[sql.Composable, list]:
    """The semantic branch: query -> nearest templates -> matching rows.

    Returns SQL producing ``(rid, rank)`` so it can be fused with the lexical
    branch by exactly the reciprocal-rank-fusion the hybrid path already uses.

    Rows are reached by joining on template text equality rather than by storing
    a template id on every row. The template column is already indexed, and the
    alternative would mean rewriting the base table whenever templates are
    rebuilt.

    ``join`` and ``where`` filter the matching rows (severity, time window) and
    refer to the index relation as ``r``; their parameters are the caller's.
    Filtering here, not after the fusion, keeps a branch's top rows from being
    spent on rows the filters then drop.
    """
    table = template_table(dataset)
    record_id = dataset.sql_for("record_id")
    template_col = dataset.sql_for("template")

    branch = sql.SQL("""
        SELECT r.{record_id} AS rid,
               ROW_NUMBER() OVER (ORDER BY m.dist, r.{record_id}) AS rank
        FROM (
            SELECT template_text, embedding <=> q.emb AS dist
            FROM {templates}, query_embedding q
            WHERE embedding <=> q.emb < {max_dist}
            ORDER BY dist
            LIMIT {width}
        ) m
        JOIN {relation} r ON {template_expr} = m.template_text
        {join}
        {where}
    """).format(
        record_id=record_id,
        templates=sql.Identifier(table),
        relation=dataset.search_relation(),
        template_expr=sql.SQL("r.{col}").format(col=template_col)
        if isinstance(template_col, sql.Identifier)
        else template_col,
        max_dist=sql.Literal(MAX_TEMPLATE_DISTANCE),
        width=sql.Literal(TEMPLATE_BRANCH_WIDTH),
        join=join or sql.SQL(""),
        where=where or sql.SQL(""),
    )
    return branch, []


def matched_templates(
    query_text: str, dataset: Dataset | str, *, limit: int = 5
) -> list[dict]:
    """Templates nearest a query, with how many rows each accounts for.

    Useful on its own, not just as a search branch: "which kinds of event look
    like this, and how common is each" is often the actual question, and it is
    answered without reading a single message body.
    """
    ds = _require_templates(dataset)
    table = template_table(ds)

    with _embed_cursor() as cur:
        cur.execute("SET sdb_ai_allow_insecure_endpoint = true")
        cur.execute(
            sql.SQL("""
                WITH q AS (
                    SELECT ai_embed(%s, {model}, {secret})::FLOAT[{dim}] AS emb
                )
                SELECT template_text, row_count, embedding <=> q.emb AS distance
                FROM {t}, q
                ORDER BY distance
                LIMIT {limit}
            """).format(
                model=sql.Literal(EMBEDDING_MODEL),
                secret=sql.Literal(EMBEDDING_SECRET),
                dim=sql.Literal(EMBEDDING_DIMENSION),
                t=sql.Identifier(table),
                limit=sql.Literal(limit),
            ),
            (query_text,),
        )
        return [
            {
                "template": r["template_text"],
                "row_count": int(r["row_count"]),
                "distance": round(float(r["distance"]), 4),
            }
            for r in cur.fetchall()
        ]
