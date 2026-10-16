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
SERENEDB_POOL_MIN = int(os.getenv("SERENEDB_POOL_MIN", "2"))
SERENEDB_POOL_MAX = int(os.getenv("SERENEDB_POOL_MAX", "32"))
SERENEDB_POOL_ACQUIRE_TIMEOUT = float(
    os.getenv("SERENEDB_POOL_ACQUIRE_TIMEOUT", "30")
)
SERENEDB_STATEMENT_TIMEOUT_MS = int(
    os.getenv("SERENEDB_STATEMENT_TIMEOUT_MS", "30000")
)

# --- Per-query-class timeouts ------------------------------------------------
# One global timeout cannot serve both an auto-complete keystroke and an
# aggregate over a billion remote rows. These are applied with ``SET LOCAL``
# inside the transaction, so they override the connection-wide default above
# per query instead of per connection.
SERENEDB_INTERACTIVE_TIMEOUT_MS = int(
    os.getenv("SERENEDB_INTERACTIVE_TIMEOUT_MS", "15000")
)
SERENEDB_BULK_TIMEOUT_MS = int(os.getenv("SERENEDB_BULK_TIMEOUT_MS", "300000"))

# Session memory ceiling, applied per connection.
#
# Must stay at or below whatever the server itself is configured with, since the
# point is to lower the ceiling, never to raise it. Measured history on this
# host: at 5.7 GiB a 2.44M-row materialization OOM-killed the process, and the
# server is now configured at 3.8 GiB. 3 GiB keeps a margin under that and makes
# an over-large query fail as a query rather than as a crash.
SERENEDB_SESSION_MEMORY_LIMIT = os.getenv("SERENEDB_SESSION_MEMORY_LIMIT", "512MB")

# Worker threads during index builds and size probes.
#
# Measured, and the opposite of the intuition that led here: reading the message
# text of one row group took 481.6s at 4 threads and 86.8s at 16 -- a 5.5x
# penalty for throttling. Decompressing Parquet is CPU-bound and parallelises
# well, so starving it of threads does not make a build safer, it just makes the
# build five times longer while holding its working set the whole time.
#
# Safety comes from ``memory_limit`` instead: bounded at 488 MiB, 16 threads
# complete without incident. Left at the server default; lower it only to yield
# CPU to another tenant, never as a memory measure.
SERENEDB_BUILD_THREADS = int(os.getenv("SERENEDB_BUILD_THREADS", "16"))

MODEL_BASE_URL = os.getenv("MODEL_BASE_URL", "http://host.docker.internal:11434")
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "nomic-embed-text:v1.5")
EMBEDDING_DIMENSION = 768

LOG_SCHEMA_PATH = Path(__file__).resolve().parent / "log_table_schema.yaml"
LOG_SCHEMA = LOG_SCHEMA_PATH.read_text(encoding="utf-8")
