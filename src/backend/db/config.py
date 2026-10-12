"""Database connection and schema configuration."""

import os
from pathlib import Path

# --- Connection target -------------------------------------------------------
SERENEDB_HOST = os.getenv("SERENEDB_HOST", "localhost")
SERENEDB_PORT = int(os.getenv("SERENEDB_PORT", "7890"))
SERENEDB_DB = os.getenv("SERENEDB_DB", "postgres")
SERENEDB_USER = os.getenv("SERENEDB_USER", "postgres")
SERENEDB_PASSWORD = os.getenv("SERENEDB_PASSWORD", "postgres")

# --- Connection pool ---------------------------------------------------------
# Each in-flight investigation issues its queries sequentially, so a modest pool
# covers many concurrent users. Keep (pool max x uvicorn workers) below SereneDB's
# max_connections.
SERENEDB_POOL_MIN = int(os.getenv("SERENEDB_POOL_MIN", "2"))
SERENEDB_POOL_MAX = int(os.getenv("SERENEDB_POOL_MAX", "32"))
# Seconds to wait for a pooled connection before failing a tool call.
SERENEDB_POOL_ACQUIRE_TIMEOUT = float(
    os.getenv("SERENEDB_POOL_ACQUIRE_TIMEOUT", "30")
)
# Server-side cap on any single query, so one slow query cannot pin a connection.
SERENEDB_STATEMENT_TIMEOUT_MS = int(
    os.getenv("SERENEDB_STATEMENT_TIMEOUT_MS", "30000")
)

MODEL_BASE_URL = os.getenv("MODEL_BASE_URL", "http://host.docker.internal:11434")
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "nomic-embed-text:v1.5")
EMBEDDING_DIMENSION = 768
TABLE_NAME = "log"

LOG_SCHEMA_PATH = Path(__file__).resolve().parent / "log_table_schema.yaml"
LOG_SCHEMA = LOG_SCHEMA_PATH.read_text(encoding="utf-8")
