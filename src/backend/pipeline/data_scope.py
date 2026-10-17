"""Build the evidence's data scope from the captured tool results, in code.

The brief requires every answer to "state the data scope and any filters" and
warns that "a few retrieved examples cannot establish how often an error
occurs". Both facts are properties of the tool results, so they are derived
here deterministically rather than asked of a model.
"""

from collections.abc import Sequence
from typing import Any

from backend.pipeline.contracts import DataScope
from backend.pipeline.tools.envelope import MEASURED_KINDS

# Filter keys that describe a time window, as (start, end) pairs.
_WINDOWS = (
    ("start_time", "end_time", None),
    ("baseline_start", "baseline_end", "baseline"),
    ("recent_start", "recent_end", "recent"),
)
_TIME_KEYS = {key for start, end, _ in _WINDOWS for key in (start, end)}
_EXAMPLE_KINDS = frozenset({"search", "context", "lookup"})


def _window(filters: dict[str, Any], start: str, end: str, label: str | None):
    if filters.get(start) is None and filters.get(end) is None:
        return None
    window = {"start": str(filters.get(start, "")), "end": str(filters.get(end, ""))}
    if label:
        window["label"] = label
    return window


def build_data_scope(records: Sequence[dict]) -> DataScope:
    """Summarize datasets, windows, filters and result kinds across records."""
    scope = DataScope()
    datasets: set[str] = set()
    for record in records:
        if not isinstance(record, dict):
            continue
        result_id = record.get("id")
        if record.get("kind") == "derived":
            # Computed in code from other records; it adds no data scope.
            continue
        if record.get("dataset"):
            datasets.add(record["dataset"])
        filters = record.get("filters")
        if filters is None:
            # Legacy envelope: fall back to the call arguments.
            filters = record.get("arguments") or {}
        for start, end, label in _WINDOWS:
            window = _window(filters, start, end, label)
            if window and window not in scope.time_windows:
                scope.time_windows.append(window)
        for key, value in filters.items():
            if key in _TIME_KEYS or key == "table_name" or value is None:
                continue
            values = scope.filters.setdefault(key, [])
            if value not in values:
                values.append(value)

        kind = record.get("kind")
        if kind in MEASURED_KINDS:
            scope.measured_result_ids.append(result_id)
        elif kind in _EXAMPLE_KINDS:
            scope.example_result_ids.append(result_id)
        if record.get("truncated") is True:
            scope.truncated_result_ids.append(result_id)
        elif record.get("truncated") is None:
            scope.unknown_truncation_result_ids.append(result_id)
    scope.datasets = sorted(datasets)
    return scope
