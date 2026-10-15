import io
import os
import subprocess

from pathlib import Path
from psycopg2 import sql

from backend.db.config import (
    EMBEDDING_DIMENSION,
    EMBEDDING_MODEL,
    LOG_CSV_PATH,
    MODEL_BASE_URL,
    TABLE_NAME,
)
from backend.db.connection import db_cursor

# The base column set shared by the source CSV and the embedded CSV. Names match
# the file's header; types map onto the log table below.
_CSV_COLUMNS = (
    ("LineId", "BIGINT"),
    ("Logrecord", "VARCHAR"),
    ("Date", "DATE"),
    ("Time", "TIME"),
    ("Pid", "BIGINT"),
    ("Level", "VARCHAR"),
    ("Service", "VARCHAR"),
    ("ADDR", "VARCHAR"),
    ("Content", "VARCHAR"),
    ("EventId", "VARCHAR"),
    ("EventTemplate", "VARCHAR"),
)

# Columns ``export_embedded_csv`` appends on top of ``_CSV_COLUMNS``: the derived
# ``LogTimestamp``/``PidText`` (so the import copies them straight across) and the
# row's ``Embedding`` vector.
_EMBEDDED_COLUMNS = (
    ("LogTimestamp", "TIMESTAMP"),
    ("PidText", "VARCHAR"),
    ("Embedding", f"FLOAT[{EMBEDDING_DIMENSION}]"),
)


def _csv_columns_clause() -> str:
    # read_csv must be told the embedded CSV's schema: passing ``columns``
    # disables the sniffer, which would otherwise read the ``Embedding`` list as
    # VARCHAR. The ``LogTimestamp``/``PidText``/``Embedding`` columns only exist in
    # the embedded CSV.
    columns = [*_CSV_COLUMNS, *_EMBEDDED_COLUMNS]
    body = ", ".join(f"'{name}': '{type_}'" for name, type_ in columns)
    return "{" + body + "}"


def create_ai_secret():
    """Registers the local Ollama AI model provider secret in SereneDB."""

    with db_cursor() as cur:
        cur.execute("DROP SECRET IF EXISTS local_ai")
        print("Dropped old secret 'local_ai' if it existed.")

        query = sql.SQL("""
            CREATE SECRET local_ai (
                TYPE openai,
                base_url {base_url},
                embeddings_path '/v1/embeddings',
                model {embedding_model}
            )
        """).format(
            base_url=sql.Literal(MODEL_BASE_URL),
            embedding_model=sql.Literal(EMBEDDING_MODEL),
        )
        cur.execute(query)
        print("AI secret 'local_ai' created successfully.")


def export_embedded_csv(file_path: str) -> None:
    """Embed a local log CSV into its ``..._embedded.csv`` sibling.

    Streams ``file_path`` (relative to this module's directory) to SereneDB,
    computes the derived ``LogTimestamp`` and ``PidText`` columns plus one
    ``ai_embed`` per row, and writes the result — the original columns plus those
    derived ones — to the same name with ``_embedded`` inserted before ``.csv``.
    """
    base_dir = Path(__file__).resolve().parent
    src_path = base_dir / file_path
    out_path = src_path.with_name(f"{src_path.stem}_embedded.csv")

    col_defs = sql.SQL(", ").join(
        sql.SQL("{} {}").format(sql.Identifier(name), sql.SQL(type_))
        for name, type_ in _CSV_COLUMNS
    )
    col_names = sql.SQL(", ").join(sql.Identifier(name) for name, _ in _CSV_COLUMNS)

    with db_cursor() as cur:
        cur.execute("SET sdb_ai_allow_insecure_endpoint = true")
        cur.execute("DROP TABLE IF EXISTS embed_src")

        create_src_query = sql.SQL("CREATE TEMP TABLE embed_src ({col_defs})").format(
            col_defs=col_defs
        )
        cur.execute(create_src_query)

        copy_in_query = sql.SQL(
            "COPY embed_src ({col_names}) FROM STDIN WITH CSV HEADER DELIMITER ','"
        ).format(col_names=col_names)

        with src_path.open("r", encoding="utf-8", newline="") as fh:
            cur.copy_expert(copy_in_query, fh)

        # The derived aliases are quoted so the CSV header keeps their casing
        # (an unquoted identifier would fold to lower case) and matches the keys
        # in ``_csv_columns_clause``.
        copy_out_query = sql.SQL(
            "COPY ("
            "  SELECT *,"
            '    (Date + Time)::TIMESTAMP AS "LogTimestamp",'
            '    CAST(Pid AS VARCHAR) AS "PidText",'
            "    ai_embed(Content, {embedding_model}, 'local_ai')"
            '::FLOAT[{embedding_dimension}] AS "Embedding"'
            "   FROM embed_src ORDER BY LineId"
            ") TO STDOUT WITH (FORMAT csv, HEADER)"
        ).format(
            embedding_model=sql.Literal(EMBEDDING_MODEL),
            embedding_dimension=sql.Literal(EMBEDDING_DIMENSION),
        )
        out = io.StringIO()
        cur.copy_expert(copy_out_query, out)

        cur.execute("DROP TABLE embed_src")

    out_path.write_text(out.getvalue(), encoding="utf-8")
    print(f"Wrote {out_path}.")


def copy_csv_to_container(file_path: str) -> None:
    """Copy a local CSV into the SereneDB container's ``/data`` directory.

    ``read_csv`` runs inside the server, so the file must live in the container;
    this is the docker-copy step from ``setup.sh``, done in-process.
    """
    base_dir = Path(__file__).resolve().parent
    src_path = base_dir / file_path
    container = os.getenv("SERENEDB_CONTAINER", "serenedb")
    remote_dir = "/data"

    subprocess.run(
        [
            "docker",
            "exec",
            "-u",
            "root",
            container,
            "sh",
            "-c",
            f"mkdir -p {remote_dir}",
        ],
        check=True,
    )
    subprocess.run(
        [
            "docker",
            "exec",
            "-u",
            "root",
            container,
            "sh",
            "-c",
            f"rm -f {remote_dir}/*.csv",
        ],
        check=True,
    )
    subprocess.run(
        ["docker", "cp", str(src_path), f"{container}:{remote_dir}/"],
        check=True,
    )
    subprocess.run(
        [
            "docker",
            "exec",
            "-u",
            "root",
            container,
            "sh",
            "-c",
            f"chmod 644 {remote_dir}/*.csv",
        ],
        check=True,
    )
    print(f"Copied {src_path.name} into container '{container}' ({remote_dir}).")


def import_from_csv(table_name: str = TABLE_NAME):
    """Build the log table from the embedded CSV that lives inside SereneDB.

    The CSV at ``LOG_CSV_PATH`` already carries the derived ``LogTimestamp`` and
    ``PidText`` columns and the ``Embedding`` vector, so there is no file COPY, no
    ``ai_embed`` step and no per-row math: rows are read straight from ``read_csv``
    and the table, full-text dictionary and inverted index are (re)built from the
    result. The reader runs server-side, so ``LOG_CSV_PATH`` must be visible to the
    SereneDB container.
    """

    with db_cursor() as cur:
        drop_query = sql.SQL("DROP TABLE IF EXISTS {table_name} CASCADE").format(
            table_name=sql.Identifier(table_name)
        )
        cur.execute(drop_query)
        print(f"Dropped old table '{table_name}' if it existed.")

        # ``log_timestamp`` and ``pid_text`` are derived by ``export_embedded_csv``
        # and imported as-is (no per-row math here). ``pid`` is an INT and only
        # text columns get a term dictionary in the inverted index, so ``pid_text``
        # is what the auto-complete suggests pids from.
        create_table_query = sql.SQL("""
            CREATE TABLE {table_name} (
                line_id INT PRIMARY KEY,
                log_record TEXT,
                log_date DATE,
                log_time TIME,
                log_timestamp TIMESTAMP,
                pid INT,
                pid_text VARCHAR,
                category VARCHAR(50),
                service TEXT,
                addr TEXT,
                content TEXT,
                event_id VARCHAR(50),
                event_template TEXT,
                embedding FLOAT[{embedding_dimension}]
            )
        """).format(
            table_name=sql.Identifier(table_name),
            embedding_dimension=sql.Literal(EMBEDDING_DIMENSION),
        )
        cur.execute(create_table_query)
        print(f"Table '{table_name}' created successfully.")

        insert_query = sql.SQL("""
            INSERT INTO {table_name} (
                line_id, log_record, log_date, log_time, log_timestamp,
                pid, pid_text, category, service, addr, content, event_id,
                event_template, embedding
            )
            SELECT
                LineId, Logrecord, Date, Time, LogTimestamp,
                Pid, PidText, Level, Service, ADDR, Content, EventId,
                EventTemplate, Embedding
            FROM read_csv({csv_path}, header = true, columns = {columns})
        """).format(
            table_name=sql.Identifier(table_name),
            csv_path=sql.Literal(LOG_CSV_PATH),
            columns=sql.SQL(_csv_columns_clause()),
        )
        cur.execute(insert_query)
        print("Loaded rows from the embedded CSV (with embeddings).")

        cur.execute("""
            CREATE TEXT SEARCH DICTIONARY IF NOT EXISTS english AS 
            split_text(case := 'lower') WITH (frequency, position, offset)
        """)

        # Service names are dotted/underscored identifiers (e.g.
        # ``nova.osapi_compute.wsgi.server``); ``split_by_non_alpha`` splits them
        # into their component words for the auto-complete's word-level mistyping
        # fallback, which matches a near-miss against those words.
        cur.execute("""
            CREATE TEXT SEARCH DICTIONARY IF NOT EXISTS dotted AS 
            split_by_non_alpha() | normalize_tokens('en_US.UTF-8', case := 'lower') 
            WITH (frequency, position)
        """)

        # ``service``, ``event_id``, ``event_template`` and ``pid_text`` are
        # indexed verbatim (no dictionary): one term per value, case intact, so the
        # auto-complete recipe's ``ts_dict_agg`` walks the dictionary and returns
        # the field's real values rather than lowercased word tokens. ``content``
        # keeps its dictionary for full-text search.
        create_index_query = sql.SQL("""
            CREATE INDEX {index_name} ON {table_name} USING inverted (
                line_id, 
                content english, 
                service, 
                event_template, 
                event_id, 
                pid_text, 
                embedding ivf (metric = 'cosine')
            )
        """).format(
            index_name=sql.Identifier(f"{table_name}_idx"),
            table_name=sql.Identifier(table_name),
        )
        cur.execute(create_index_query)

        # The auto-complete's mistyping fallback needs the same columns split into
        # words, so a near-miss on one word still matches the row.
        create_token_index_query = sql.SQL("""
            CREATE INDEX {index_name} ON {table_name} USING inverted (
                service dotted, 
                event_id english, 
                event_template english,
                pid_text english
            )
        """).format(
            index_name=sql.Identifier(f"{table_name}_token_idx"),
            table_name=sql.Identifier(table_name),
        )
        cur.execute(create_token_index_query)

        refresh_query = sql.SQL("VACUUM (REFRESH_TABLE) {table_name}").format(
            table_name=sql.Identifier(table_name)
        )
        cur.execute(refresh_query)
        print("Created the text search dictionary and inverted index.")


if __name__ == "__main__":
    ### CREATE AI SECRET REQUIRED TO RUN SERENEDB, ONLY NEED TO BE RAN ONCE
    create_ai_secret()

    ### EMBED THE LOCAL CSV
    # export_embedded_csv("../data/OpenStack_2k.log_structured.csv")

    ### COPY THE EMBEDDED CSV INTO THE SERENEDB CONTAINER
    copy_csv_to_container("../data/OpenStack_2k.log_structured_embedded.csv")

    ### IMPORT FROM THE EMBEDDED CSV THAT LIVES IN THE SERENEDB CONTAINER
    import_from_csv()
