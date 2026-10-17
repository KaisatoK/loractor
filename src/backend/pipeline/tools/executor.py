"""Execution wrapper for database-backed agent tools."""

import logging
from collections.abc import Callable
from typing import Any

from langchain.tools import ToolRuntime

from backend.pipeline.tools.envelope import run_enveloped

logger = logging.getLogger(__name__)


class ProgressReporter:
    """Lets a long-running tool report what it is doing while it runs.

    Most tools are one query and need nothing of the sort: the ``started`` and
    ``completed`` events around them tell the whole story. The sweep is the
    exception -- it iterates over up to 1,624 chunks inside a *single* tool call,
    because per-effort budgets cap all tool calls at between 4 and 20. Without a
    way to report mid-flight, the most expensive operation in the system would
    be its most opaque: minutes of silence, then a result.

    Events carry a ``stage`` and a ``detail`` payload alongside the message, so
    a reader can see not just that the sweep is working but which pass it is on
    and what it decided.
    """

    def __init__(self, writer, operation: str):
        self._writer = writer
        self._operation = operation
        self.events: list[dict] = []

    def __call__(
        self,
        message: str,
        *,
        stage: str = "working",
        detail: dict | None = None,
    ) -> None:
        payload = {
            "type": "tool_progress",
            "operation": self._operation,
            "status": "working",
            "stage": stage,
            "message": message,
        }
        if detail:
            payload["detail"] = detail
        self.events.append(payload)
        self._writer(payload)
        # Also logged, so `LOG_LEVEL=DEBUG bash ./backend.sh` shows the
        # iteration in the terminal without a UI attached.
        logger.info("[%s] %s", stage, message)


def execute_database_tool(
    *,
    runtime: ToolRuntime,
    operation: str,
    function: Callable[[], Any],
    kind: str | None = None,
    dataset: str | None = None,
    filters: dict[str, Any] | None = None,
    limit: int | None = None,
    complete: bool = False,
    with_reporter: bool = False,
) -> dict:
    """Run a database function and emit tool-progress events around it.

    Three result shapes reach the agent, and the distinction matters:

    * **success** -- the tool-result envelope (``tools/envelope.py``): rows plus
      ``kind``, ``dataset``, the executed ``query``, ``filters``, ``limit``,
      ``truncated`` and, when the query layer measured it, ``query_stats`` (what
      the query cost). ``complete=True`` marks a query without a row cap.
    * **refused** -- the query was never run because it was unbounded, too wide,
      or asked for a capability the corpus lacks. Carries a concrete suggestion,
      so the agent's next move is to narrow and retry rather than to give up or
      hallucinate. This is *not* an error: it is a useful, cheap answer.
    * **failure** -- re-raised as a generic ``RuntimeError`` so
      ``handle_tool_errors`` turns it into a ToolMessage.

    Progress events go to the graph's custom stream. They only reach the UI from
    depth <= 1, so tools must stay on the main agent.

    ``with_reporter`` hands ``function`` a :class:`ProgressReporter`. Only the
    sweep uses it: it is the one tool whose single call contains a long internal
    loop, and reporting from inside that loop is the difference between an
    observable operation and a blank wait. The emitted events are also kept on
    the result as ``progress_trace`` so they remain inspectable after the run,
    not only while it is streaming.
    """
    writer = runtime.stream_writer
    reporter = ProgressReporter(writer, operation)
    writer(
        {
            "type": "tool_progress",
            "operation": operation,
            "status": "started",
            "message": f"Starting {operation}.",
        }
    )
    logger.info("Executing database tool: %s", operation)

    try:
        envelope = run_enveloped(
            operation=operation,
            function=(lambda: function(reporter)) if with_reporter else function,
            kind=kind,
            dataset=dataset,
            filters=filters,
            limit=limit,
            complete=complete,
        )
    except Exception as e:
        logger.exception("Database tool failed: %s", operation)
        writer(
            {
                "type": "tool_progress",
                "operation": operation,
                "status": "failed",
                "message": f"{operation} failed.",
            }
        )
        raise RuntimeError(f"{operation} failed") from e

    if envelope.get("status") == "refused":
        # A refusal is a legitimate, informative outcome, so it is reported as
        # 'skipped' rather than 'failed': nothing broke, the request was just
        # too expensive or unsupported as asked.
        logger.info("Refused %s: %s", operation, envelope.get("reason"))
        writer(
            {
                "type": "tool_progress",
                "operation": operation,
                "status": "skipped",
                "message": f"{operation} refused: {envelope.get('reason')}",
            }
        )
        return envelope

    stats = envelope.get("query_stats")
    summary = _stats_summary(stats)
    writer(
        {
            "type": "tool_progress",
            "operation": operation,
            "status": "completed",
            "message": (
                f"Completed {operation} ({summary})." if summary else f"Completed {operation}."
            ),
        }
    )
    if reporter.events:
        envelope["progress_trace"] = [
            {
                "stage": event["stage"],
                "message": event["message"],
                **({"detail": event["detail"]} if "detail" in event else {}),
            }
            for event in reporter.events
        ]
    logger.debug("Tool query for %s: %s", operation, envelope.get("query"))
    return envelope


def _stats_summary(stats: dict[str, Any] | None) -> str | None:
    """The one-line cost summary of a ``query_stats`` dict, or ``None``."""
    if not stats:
        return None
    from backend.db.stats import QueryStats  # lazy: keeps the import graph flat

    fields = {k: v for k, v in stats.items() if k in QueryStats.__dataclass_fields__}
    try:
        return QueryStats(**fields).summary()
    except TypeError:
        return None
