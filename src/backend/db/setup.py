from psycopg2 import sql
from pathlib import Path

from utils import (
    db_cursor,
    MODEL_BASE_URL,
    EMBEDDING_MODEL,
    EMBEDDING_DIMENSION,
    TABLE_NAME,
)


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


def import_from_csv(relative_file_path: str, table_name: str = TABLE_NAME):
    base_dir = Path(__file__).resolve().parent
    absolute_file_path = base_dir / relative_file_path

    with db_cursor() as cur:
        cur.execute("SET sdb_ai_allow_insecure_endpoint = true")

        drop_query = sql.SQL("DROP TABLE IF EXISTS {table_name} CASCADE").format(
            table_name=sql.Identifier(table_name)
        )
        cur.execute(drop_query)
        print(f"Dropped old table '{table_name}' if it existed.")

        create_table_query = sql.SQL("""
            CREATE TABLE {table_name} (
                line_id INT PRIMARY KEY,
                log_record TEXT,
                log_date DATE,
                log_time TIME,
                log_timestamp TIMESTAMP,
                pid INT,
                category VARCHAR(50),
                component TEXT,
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

        copy_query = sql.SQL("""
            COPY {table_name} (
                line_id, log_record, log_date, log_time, pid, 
                category, component, addr, content, event_id, event_template
            )
            FROM STDIN
            WITH CSV HEADER DELIMITER ','
        """).format(table_name=sql.Identifier(table_name))
        with open(absolute_file_path, "r", encoding="utf-8") as f:
            cur.copy_expert(copy_query, f)
        print("CSV data copied successfully.")

        update_timestamp_query = sql.SQL("""
            UPDATE {table_name} 
            SET log_timestamp = (log_date + log_time)::timestamp
        """).format(table_name=sql.Identifier(table_name))
        cur.execute(update_timestamp_query)
        print("Populated log_timestamp column.")

        update_embedding_query = sql.SQL("""
            UPDATE {table_name} 
            SET embedding = ai_embed(content, {embedding_model}, 'local_ai')::FLOAT[{embedding_dimension}]
        """).format(
            table_name=sql.Identifier(table_name),
            embedding_model=sql.Literal(EMBEDDING_MODEL),
            embedding_dimension=sql.Literal(EMBEDDING_DIMENSION),
        )
        cur.execute(update_embedding_query)
        print("Generated embedding vectors.")

        cur.execute("""
            CREATE TEXT SEARCH DICTIONARY IF NOT EXISTS english AS 
            split_text(case := 'lower') WITH (frequency, position, offset)
        """)

        create_index_query = sql.SQL("""
            CREATE INDEX {index_name} ON {table_name} USING inverted (
                line_id, 
                content english, 
                embedding ivf (metric = 'cosine')
            )
        """).format(
            index_name=sql.Identifier(f"{table_name}_idx"),
            table_name=sql.Identifier(table_name),
        )
        cur.execute(create_index_query)

        refresh_query = sql.SQL("VACUUM (REFRESH_TABLE) {table_name}").format(
            table_name=sql.Identifier(table_name)
        )
        cur.execute(refresh_query)
        print("Created the text search dictionary and inverted index.")


if __name__ == "__main__":
    ### CREATE AI SECRET REQUIRED TO RUN SERENEDB, ONLY NEED TO BE RAN ONCE
    create_ai_secret()

    ### IMPORT FROM CSV
    import_from_csv("../csv/OpenStack_2k.log_structured.csv")
