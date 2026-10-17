"""Database access layer: connection, configuration, datasets, and log queries.

The query surface is **dataset-driven**: every builder takes a ``dataset`` id
resolved through :mod:`backend.db.datasets` and composes SQL from canonical
field names, so a new corpus is a registration rather than a new set of
queries. See that module for the canonical field vocabulary.
"""

from backend.db.auto_complete import auto_complete
from backend.db.budget import BUDGET, CORPUS_FACTS, fits
from backend.db.catalog_query import list_datasets
from backend.db.config import (
    EMBEDDING_DIMENSION,
    EMBEDDING_MODEL,
    LOG_SCHEMA,
    MODEL_BASE_URL,
    SERENEDB_PORT,
)
from backend.db.connection import bulk_query, db_cursor, get_conn, query_runner
from backend.db.count_query import (
    compare_periods,
    count_by_dataset,
    count_by_field,
    count_distinct_by_field,
    top_by_field,
)
from backend.db.datasets import (
    Capability,
    Dataset,
    DatasetKind,
    all_datasets,
    catalog,
    dataset_ids,
    resolve,
)
from backend.db.errors import QueryRefused, Refusal
from backend.db.list_query import get_field_values
from backend.db.query import get_log_by_record_id, search_logs
from backend.db.stats import QueryClass, QueryResult, QueryStats
from backend.db.trend_query import get_trend

__all__ = [
    "BUDGET",
    "CORPUS_FACTS",
    "Capability",
    "Dataset",
    "DatasetKind",
    "EMBEDDING_DIMENSION",
    "EMBEDDING_MODEL",
    "LOG_SCHEMA",
    "MODEL_BASE_URL",
    "QueryClass",
    "QueryRefused",
    "QueryResult",
    "QueryStats",
    "Refusal",
    "SERENEDB_PORT",
    "all_datasets",
    "auto_complete",
    "bulk_query",
    "catalog",
    "compare_periods",
    "count_by_dataset",
    "count_by_field",
    "count_distinct_by_field",
    "dataset_ids",
    "db_cursor",
    "fits",
    "get_conn",
    "get_field_values",
    "get_log_by_record_id",
    "get_trend",
    "list_datasets",
    "query_runner",
    "resolve",
    "search_logs",
    "top_by_field",
]
