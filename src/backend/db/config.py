"""Database connection and schema configuration."""

from pathlib import Path

SERENEDB_PORT = 7890
MODEL_BASE_URL = "http://host.docker.internal:11434"
EMBEDDING_MODEL = "nomic-embed-text:v1.5"
EMBEDDING_DIMENSION = 768
TABLE_NAME = "log"

LOG_SCHEMA_PATH = Path(__file__).resolve().parent / "log_table_schema.yaml"
LOG_SCHEMA = LOG_SCHEMA_PATH.read_text(encoding="utf-8")
