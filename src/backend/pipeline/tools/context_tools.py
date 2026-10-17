"""Context tool: nearby events around one anchor line.

The engineer's "what happened just before and after?". Rows are kind
``context``: examples for a timeline, never a frequency. The description is
``schemas.NEARBY_EVENTS_DESCRIPTION``, shared with the MCP wrapper and the MCP
server, and the operation label and filters come from the helpers below, so
every transport reports the call the same way.
"""

from langchain.tools import ToolRuntime, tool

from backend.db import get_nearby_events
from backend.db.datasets import DEFAULT_DATASET_ID
from backend.pipeline.tools.executor import execute_database_tool
from backend.pipeline.tools.schemas import NEARBY_EVENTS_DESCRIPTION, GetNearbyEvents


def nearby_operation(line_id: int, dataset: str, before: int, after: int,
                     same_entity: bool, same_request: bool) -> str:
    """The call's human label, shared by every transport."""
    operation = f"fetch {before} lines before and {after} after {dataset} line {line_id}"
    narrowed = [label for label, wanted in (("entity", same_entity),
                                            ("request", same_request)) if wanted]
    if narrowed:
        operation += f" with the same {' and '.join(narrowed)}"
    return operation


def nearby_filters(line_id: int, before: int, after: int,
                   same_entity: bool, same_request: bool) -> dict:
    """The applied arguments, as the result's ``filters``."""
    return {
        "line_id": line_id,
        "before": before,
        "after": after,
        "same_entity": same_entity,
        "same_request": same_request,
    }


@tool(args_schema=GetNearbyEvents, description=NEARBY_EVENTS_DESCRIPTION)
def get_nearby_events_tool(
    line_id: int,
    runtime: ToolRuntime,
    dataset: str = DEFAULT_DATASET_ID,
    before: int = 5,
    after: int = 5,
    same_entity: bool = False,
    same_request: bool = False,
) -> dict:
    return execute_database_tool(
        runtime=runtime,
        operation=nearby_operation(line_id, dataset, before, after,
                                   same_entity, same_request),
        function=lambda: get_nearby_events(
            line_id, dataset, before, after, same_entity, same_request
        ),
        kind="context",
        dataset=dataset,
        # The row count is what was asked for, not a cap that cut anything off.
        complete=True,
        filters=nearby_filters(line_id, before, after, same_entity, same_request),
    )
