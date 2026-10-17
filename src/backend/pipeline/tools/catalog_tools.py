"""Dataset catalog tool.

Every windowed tool needs a time window, and without this the agent can only
guess one. The catalog is measured on every call -- row counts, first and last
timestamps and severity levels per dataset -- so windows and severity filters
come from the data. Result kind ``catalog``: discovery, not a frequency over the
investigated scope.
"""

from langchain.tools import ToolRuntime, tool

from backend.db import list_datasets
from backend.pipeline.tools.executor import execute_database_tool
from backend.pipeline.tools.schemas import ListDatasets

OPERATION = "list every dataset with its rows, time span and severity levels"


@tool(args_schema=ListDatasets)
def list_datasets_tool(runtime: ToolRuntime) -> dict:
    """List every dataset's rows, time span and severity levels, measured now.

    No inputs. Returns kind=catalog, one row per dataset: rows, first_timestamp,
    last_timestamp, max_window_seconds, window_start/window_end (a window that
    covers every row; pass them as start_time/end_time) and levels
    [{severity, count}]. available=false means not loaded or not built. Call it
    first when the user named no time window.
    """
    return execute_database_tool(
        runtime=runtime,
        operation=OPERATION,
        function=list_datasets,
        kind="catalog",
        # One row per registered dataset, no cap: the result is complete.
        complete=True,
    )
