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
import time

from psycopg2 import sql

from backend.db.config import EMBEDDING_DIMENSION, EMBEDDING_MODEL
from backend.db.datasets import Capability, Dataset, resolve
from backend.db.errors import refuse

logger = logging.getLogger(__name__)

# Templates embedded per statement. Each row makes its own HTTP call to the
# embedding service from inside the server, so a batch bounds how much work one
# statement holds open rather than how much memory it needs.
EMBED_BATCH = 200

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


def build_templates(dataset: Dataset | str, *, rebuild: bool = False) -> dict:
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
        started = time.monotonic()
        cur.execute(
            sql.SQL("""
                CREATE OR REPLACE TEMP VIEW _pending AS
                SELECT {template} AS template_text, count(*) AS row_count
                FROM {source}
                GROUP BY 1
            """).format(template=template_sql, source=source)
        )
        cur.execute("SELECT count(*) AS n FROM _pending")
        total = int(cur.fetchone()["n"])
        report["distinct_templates"] = total
        report["extract_seconds"] = round(time.monotonic() - started, 2)

        cur.execute(
            sql.SQL("SELECT count(*) AS n FROM {t}").format(t=sql.Identifier(table))
        )
        already = int(cur.fetchone()["n"])

        # Embed only what is missing, so an interrupted build resumes instead of
        # paying for every template again.
        started = time.monotonic()
        embedded = 0
        while True:
            cur.execute(
                sql.SQL("""
                    INSERT INTO {t} (template_id, template_text, row_count, embedding)
                    SELECT
                        {offset} + row_number() OVER (),
                        p.template_text,
                        p.row_count,
                        ai_embed(p.template_text, {model}, 'local_ai')
                               ::FLOAT[{dim}]
                    FROM _pending p
                    WHERE NOT EXISTS (
                        SELECT 1 FROM {t} e WHERE e.template_text = p.template_text
                    )
                    LIMIT {batch}
                """).format(
                    t=sql.Identifier(table),
                    offset=sql.Literal(already + embedded),
                    model=sql.Literal(EMBEDDING_MODEL),
                    dim=sql.Literal(EMBEDDING_DIMENSION),
                    batch=sql.Literal(EMBED_BATCH),
                )
            )
            inserted = max(cur.rowcount, 0)
            embedded += inserted
            logger.info(
                "Embedded %d/%d templates for %s", already + embedded, total, ds.id
            )
            if inserted == 0:
                break

        report["embedded"] = embedded
        report["already_present"] = already
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

    return {
        "exists": True,
        "dataset": ds.id,
        "table": table,
        "templates": int(row["templates"]),
        "embedded": int(row["embedded"] or 0),
        "rows_covered": int(row["rows_covered"] or 0),
        "complete": int(row["embedded"] or 0) == int(row["templates"]),
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
    except Exception:  # noqa: BLE001 - absence must never break search
        logger.debug("Template status unavailable for %s", ds.id, exc_info=True)
        available = False
    _VECTOR_CACHE[ds.id] = available
    return available


def template_branch(dataset: Dataset) -> tuple[sql.Composable, list]:
    """The semantic branch: query -> nearest templates -> matching rows.

    Returns SQL producing ``(rid, rank)`` so it can be fused with the lexical
    branch by exactly the reciprocal-rank-fusion the hybrid path already uses.

    Rows are reached by joining on template text equality rather than by storing
    a template id on every row. The template column is already indexed, and the
    alternative would mean rewriting the base table whenever templates are
    rebuilt.
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
    """).format(
        record_id=record_id,
        templates=sql.Identifier(table),
        relation=dataset.search_relation(),
        template_expr=sql.SQL("r.{col}").format(col=template_col)
        if isinstance(template_col, sql.Identifier)
        else template_col,
        max_dist=sql.Literal(MAX_TEMPLATE_DISTANCE),
        width=sql.Literal(TEMPLATE_BRANCH_WIDTH),
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
                    SELECT ai_embed(%s, {model}, 'local_ai')::FLOAT[{dim}] AS emb
                )
                SELECT template_text, row_count, embedding <=> q.emb AS distance
                FROM {t}, q
                ORDER BY distance
                LIMIT {limit}
            """).format(
                model=sql.Literal(EMBEDDING_MODEL),
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
