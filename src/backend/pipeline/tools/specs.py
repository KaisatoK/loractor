"""The one place every evidence tool is declared.

A tool used to be written out three times -- a local LangChain tool
(``tools/*.py``), a wrapper that calls the MCP server (``mcp_tools.py``) and the
server's own tool (``mcp_server/server.py``) -- each with its own description,
operation label and filter dict, plus a fourth hand-written line in the
classifiers' ``TOOL_CATALOG``. They drifted. Now a tool is one :class:`ToolSpec`
in :data:`SPECS`, and every transport is generated from it:

* ``tools/local.py``      the in-process LangChain tools (the default transport);
* ``tools/mcp_tools.py``  the pipeline's wrappers that call the MCP server;
* ``mcp_server/tools.py`` the MCP server's tools;
* ``envelope.TOOL_KINDS`` and ``prompts.TOOL_CATALOG`` are derived from the table.

``agent_specs()`` is what the pipeline's agent offers; ``server_specs()`` is what
the MCP server offers (every available spec, including server-only ones such as
``rank_priorities_tool``); ``pipeline_specs()`` is what the pipeline may declare
(planned tools included), and it is the key set of ``TOOL_CATALOG`` and
``tools.__all__``.

What lives where:

* argument names, bounds, enums and their descriptions: ``tools/schemas.py``
  (``ToolSpec.args_model``) -- described once, in the parameter schema;
* a tool's ``description``: purpose, when to use it and which tool instead, what
  it returns, its limits and what an empty result means. It does not repeat the
  inputs;
* how a call runs: the DB function, the human ``operation`` label and the applied
  ``filters`` (which feed the answer's data scope);
* the result contract: ``tools/envelope.py``.

Adding a tool is one DB function plus one entry here; see AGENTS.md
"Integrating new tools and tables". This module deliberately does not import
``envelope`` (the envelope derives ``TOOL_KINDS`` from it).

Order matters in two places and is kept stable on purpose: the table follows the
order of ``TOOL_CATALOG`` (the classifiers' input, which the live route cases
are coupled to), and the evidence agent gets the same order with the sweep
tools last.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from backend.db import (
    add_burst_summary,
    compare_periods,
    count_by_dataset,
    count_by_field,
    count_by_team,
    get_field_values,
    get_log_by_record_id,
    get_nearby_events,
    get_trend,
    list_datasets,
    top_by_field,
)
from backend.db.query import search_with_total
from backend.db.stats import QueryResult, QueryStats, merge
from backend.pipeline.sweep import (
    SweepBudget,
    estimate_sweep,
    investigate_chunk,
    sweep,
)
from backend.pipeline.tools import schemas

# ``run(args, progress)``: ``args`` are the validated arguments (defaults filled
# in); ``progress`` is a ``ProgressReporter`` for the one tool that reports from
# inside itself, else ``None``.
RunFn = Callable[[dict[str, Any], Any], Any]


@dataclass(frozen=True)
class ToolSpec:
    """Everything one evidence tool is, on every transport."""

    name: str
    title: str
    description: str
    kind: str
    # The classifiers' one-line view of the tool (``TOOL_CATALOG``): a tier tag
    # plus when to reach for it. Distinct from ``description``, which is what the
    # agent and external clients read.
    catalog: str
    args_model: type[BaseModel] | None
    run: RunFn | None
    operation: Callable[[dict[str, Any]], str]
    filters: Callable[[dict[str, Any]], dict[str, Any]] = lambda args: {}
    # The envelope's ``dataset`` and ``limit``; most tools read them from the
    # arguments of the same name.
    dataset_of: Callable[[dict[str, Any]], str | None] = lambda args: args.get("dataset")
    limit_of: Callable[[dict[str, Any]], int | None] = lambda args: args.get("limit")
    # True for a query without a row cap (grouped counts, trends, key lookups).
    complete: bool = False
    # Only the sweep reports from inside its single long call.
    reports_progress: bool = False
    # Whether the tool can read the remote corpus (MCP ``openWorldHint``).
    open_world: bool = True
    # False for tools that build and drop an index on disk (the sweep): clients
    # then ask before running them.
    read_only: bool = True
    # Withheld from agents below Extreme effort (``find_evidence``).
    sweep: bool = False
    # Planned tools (docs/tool_requests.md) are declared so the catalog, kinds and
    # schemas exist, but no transport offers them until their owner flips this.
    available: bool = True
    # False for a tool that only the MCP server offers (``rank_priorities_tool``):
    # an external agent has no ``answer_node``, so it needs the ranking, while the
    # pipeline adds its own derived record. A server-only tool is absent from the
    # local tools, the pipeline's MCP wrappers, ``tools.__all__`` and the
    # classifiers' ``TOOL_CATALOG``; it keeps its entry in ``TOOL_KINDS``.
    pipeline: bool = True
    # Seconds the MCP server allows one call; None = unbounded (the sweep).
    timeout: float | None = 45.0

    def envelope_kwargs(self, args: dict[str, Any]) -> dict[str, Any]:
        """The arguments of ``run_enveloped`` / ``execute_database_tool``."""
        return {
            "operation": self.operation(args),
            "kind": self.kind,
            "dataset": self.dataset_of(args),
            "filters": self.filters(args),
            "limit": self.limit_of(args),
            "complete": self.complete,
        }


# --- Labels and filters shared by several tools ----------------------------------
_COUNT_LABELS = {
    "general": "logs",
    "service": "services",
    "entity_id": "entities",
    "event_id": "event ids",
    "template": "event templates",
}
_TREND_LABELS = {
    "service": "service",
    "entity_id": "entity",
    "event_id": "event id",
    "template": "template",
    "severity": "severity",
}


def search_operation(query_text: str, dataset: str, category: str | None,
                     start_time: str | None, end_time: str | None) -> str:
    operation = f"search {dataset} messages for '{query_text}'"
    if category:
        operation += f" with severity '{category}'"
    if start_time and end_time:
        operation += f" from {start_time} to {end_time}"
    return operation


def search_filters(query_text: str, category: str | None,
                   start_time: str | None, end_time: str | None) -> dict:
    """The applied filters; the window keys feed the answer's data scope."""
    return {
        "query_text": query_text,
        "category": category,
        "start_time": start_time,
        "end_time": end_time,
    }


def trend_operation(field: str, value: str, dataset: str,
                    category: str | None, group_by: str | None) -> str:
    operation = f"get the {_TREND_LABELS.get(field, field)} trend for '{value}' on {dataset}"
    if category:
        operation += f" by '{category}' severity"
    if group_by:
        operation += f" per {group_by}"
    return operation


def trend_filters(start_time: str, end_time: str, field: str, value: str,
                  category: str | None, bucket: str | None,
                  group_by: str | None) -> dict:
    return {
        "start_time": start_time,
        "end_time": end_time,
        field: value,
        "category": category,
        # 'auto' when the window chose it; query_stats.notes names the bucket.
        "bucket": bucket or "auto",
        "group_by": group_by,
    }


def count_by_dataset_operation(datasets: list[str] | None) -> str:
    scope = ", ".join(datasets) if datasets else "every local dataset"
    return f"count log entries by severity in {scope}"


def count_by_team_operation(datasets: list[str] | None, start_time: str | None,
                            end_time: str | None, category: str | None) -> str:
    scope = ", ".join(datasets) if datasets else "every local dataset"
    operation = f"count log entries by owning team and severity in {scope}"
    if start_time and end_time:
        operation += f" from {start_time} to {end_time}"
    if category:
        operation += f" for '{category}'"
    return operation


def rank_priorities_operation(args: dict) -> str:
    datasets = args["datasets"]
    scope = ", ".join(datasets) if datasets else "every local dataset"
    return (
        f"rank {args['group_by']} problems in {scope} from "
        f"{args['baseline_start']} to {args['baseline_end']} against "
        f"{args['recent_start']} to {args['recent_end']}"
    )


def rank_priorities_filters(args: dict) -> dict:
    return {
        "datasets": args["datasets"],
        "group_by": args["group_by"],
        "baseline_start": args["baseline_start"],
        "baseline_end": args["baseline_end"],
        "recent_start": args["recent_start"],
        "recent_end": args["recent_end"],
    }


def nearby_operation(line_id: int, dataset: str, before: int, after: int,
                     same_entity: bool, same_request: bool) -> str:
    operation = f"fetch {before} lines before and {after} after {dataset} line {line_id}"
    narrowed = [label for label, wanted in (("entity", same_entity),
                                            ("request", same_request)) if wanted]
    if narrowed:
        operation += f" with the same {' and '.join(narrowed)}"
    return operation


# --- The sweep's two adapters ------------------------------------------------------
def _relay(reporter):
    """Forward sweep events to the tool-progress stream.

    A thin adapter on purpose: the sweep should not know what a tool runtime is,
    and the reporter should not know what a sweep stage is.
    """

    def relay(event):
        reporter(event.message, stage=event.stage, detail=event.detail)

    return relay


def _sweep_estimate(dataset: str, budget: SweepBudget) -> QueryResult:
    """An estimate as a one-row result: an estimate is genuinely one record, so
    the display path stays unchanged rather than learning a second result shape."""
    estimate = estimate_sweep(dataset, budget=budget)
    estimate["confirm_required"] = True
    estimate["how_to_proceed"] = (
        "Show these figures to the user. If they agree, call again with "
        "confirm_cost=true."
    )
    return QueryResult([estimate], stats=None)


def _sweep_rows(result) -> QueryResult:
    """Flatten a sweep result into rows, keeping its stats for cost reporting."""
    payload = result.as_dict()
    rows = [
        {
            "coverage": payload["coverage"],
            "progress": payload["progress"],
            "cursor": payload["cursor"],
            "notes": payload["notes"],
            # How the sweep reached its conclusions, step by step. The model is
            # told to report coverage; this is what lets a human check it.
            "trace": payload["trace"],
            # What each sample supports. The sample is a cheap guess at a chunk
            # nothing is known about, so its confidence travels with it.
            "sampling": payload["sampling"],
        }
    ]
    rows += [{"ranked_chunk": entry} for entry in payload["ranked"]]
    rows += [{"digest": digest} for digest in payload["digests"]]
    return QueryResult(rows, stats=result.stats)


# --- run functions -----------------------------------------------------------------
# Each reads the DB function through this module's globals, so a test replaces one
# by patching ``specs.<name>`` -- once, for every transport.
def _run_list_datasets(args, progress):
    return list_datasets()


def _run_count_by_dataset(args, progress):
    datasets = args["datasets"]
    return count_by_dataset(tuple(datasets) if datasets else None)


def _run_count_by_category(args, progress):
    group_by = None if args["type"] == "general" else args["type"]
    return count_by_field(
        args["start_time"], args["end_time"], group_by, args["dataset"],
        args["category"], args["limit"],
    )


def _run_search(args, progress):
    return search_with_total(
        args["query_text"], args["dataset"], args["category"], args["limit"],
        args["start_time"], args["end_time"],
    )


def _run_line_id(args, progress):
    return get_log_by_record_id(args["line_id"], args["dataset"])


def _run_top(args, progress):
    return top_by_field(
        args["start_time"], args["end_time"], args["field"], args["dataset"],
        args["category"], args["limit"],
    )


def _run_compare(args, progress):
    return compare_periods(
        args["baseline_start"], args["baseline_end"], args["recent_start"],
        args["recent_end"], args["dataset"], args["field"], args["limit"],
    )


def _run_values(args, progress):
    return get_field_values(
        args["start_time"], args["end_time"], args["type"], args["dataset"],
        args["limit"],
    )


def _run_trend(args, progress):
    field, value = args["type"]
    # The burst summary (consecutive non-zero bucket runs and their totals) is
    # derived in code from the returned grid, so it costs no second query.
    return add_burst_summary(
        get_trend(
            args["start_time"], args["end_time"], field, value, args["dataset"],
            args["bucket"], args["category"], args["group_by"],
        ),
        group_by=args["group_by"],
    )


def _run_nearby(args, progress):
    return get_nearby_events(
        args["line_id"], args["dataset"], args["before"], args["after"],
        args["same_entity"], args["same_request"],
    )


def _run_count_by_team(args, progress):
    datasets = args["datasets"]
    return count_by_team(
        datasets=tuple(datasets) if datasets else None,
        start_time=args["start_time"],
        end_time=args["end_time"],
        category=args["category"],
    )


def _run_rank_priorities(args, progress):
    """Fetch the per-window counts, then score them in ``pipeline.ranking``.

    Two ``count_by_field`` calls per dataset (or one ``count_by_team`` call per
    window in team mode) produce the same per-window counts the pipeline's own
    ``count_by_category_tool`` / ``count_by_team_tool`` would; the scoring, and
    the trust rule for a value missing from a capped window, live in
    ``pipeline/ranking.py``, which has no database access.
    """
    from backend.db.datasets import local_datasets, resolve
    from backend.pipeline import ranking  # lazy: priority -> envelope -> specs is cyclic
    from backend.pipeline.playbooks import team_for

    datasets = args["datasets"] or sorted(d.id for d in local_datasets())
    dimension = args["group_by"]
    windows = (
        ("baseline", args["baseline_start"], args["baseline_end"]),
        ("recent", args["recent_start"], args["recent_end"]),
    )
    rows_by_window: dict[str, dict[str, list]] = {"baseline": {}, "recent": {}}
    cut_by_window: dict[str, dict[str, bool]] = {"baseline": {}, "recent": {}}
    approximate: dict[str, bool] = {}
    stats: list[QueryStats] = []

    if dimension == "team":
        for label, start, end in windows:
            result = count_by_team(
                datasets=tuple(datasets), start_time=start, end_time=end
            )
            if result.refused:
                return result
            for row in result:
                rows_by_window[label].setdefault(row["dataset"], []).append(row)
            # count_by_team has no row cap, so no window here is cut.
            cut_by_window[label] = {dataset_id: False for dataset_id in datasets}
            stats.append(result.stats)
    else:
        for dataset_id in datasets:
            approximate[dataset_id] = not resolve(dataset_id).exact_counts
            for label, start, end in windows:
                result = count_by_field(
                    start, end, dimension, dataset_id, None, ranking.CANDIDATE_GROUPS
                )
                if result.refused:
                    return result
                rows_by_window[label][dataset_id] = list(result)
                cut_by_window[label][dataset_id] = (
                    len(result) >= ranking.CANDIDATE_GROUPS
                )
                stats.append(result.stats)

    combined = ranking.rank_all(
        [
            {
                "dataset": dataset_id,
                "baseline_rows": rows_by_window["baseline"].get(dataset_id, []),
                "recent_rows": rows_by_window["recent"].get(dataset_id, []),
                "baseline_cut": cut_by_window["baseline"].get(dataset_id, False),
                "recent_cut": cut_by_window["recent"].get(dataset_id, False),
                "approximate": approximate.get(dataset_id, False),
            }
            for dataset_id in datasets
        ],
        dimension=dimension,
        limit=args["limit"],
        team_for=team_for,
    )
    merged = merge(stats) if stats else QueryStats()
    merged.notes.extend(combined["notes"])
    return QueryResult(
        combined["rows"], stats=merged, total_matches=combined["total_ranked"]
    )


def _run_sweep(args, progress):
    budget = SweepBudget(max_promotions=args["max_promotions"])
    if not args["confirm_cost"]:
        return _sweep_estimate(args["dataset"], budget)
    return _sweep_rows(
        sweep(
            args["question"],
            dataset=args["dataset"],
            budget=budget,
            start_at=args["start_at"],
            progress=_relay(progress) if progress is not None else None,
        )
    )


def _run_investigate(args, progress):
    return QueryResult(
        [
            investigate_chunk(
                args["chunk_id"], question=args["question"], dataset=args["dataset"]
            )
        ],
        stats=None,
    )


def _sweep_operation(args) -> str:
    if not args["confirm_cost"]:
        return f"estimate the cost of sweeping {args['dataset']}"
    return (
        f"sweep {args['dataset']} for evidence about {args['question'][:60]!r}"
        + (f" with {args['max_promotions']} promotions" if args["max_promotions"] else "")
    )


def _sweep_filters(args) -> dict:
    if not args["confirm_cost"]:
        return {"question": args["question"], "confirm_cost": False}
    return {
        "question": args["question"],
        "max_promotions": args["max_promotions"],
        "start_at": args["start_at"],
    }


def _rank_operation(args) -> str:
    operation = f"rank {args['dataset']} {args['field']} by log volume"
    if args["category"]:
        operation += f" for '{args['category']}' severity"
    return operation


# --- The table ---------------------------------------------------------------------
SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="list_datasets_tool",
        title="List datasets",
        description=(
            "List every registered dataset with what it holds, measured on this "
            "call. Call it first, when the user named no time window, so windows "
            "and severity filters come from the data instead of a guess. Takes no "
            "arguments. Returns kind=catalog, one row per dataset: dataset, kind, "
            "available (false = not loaded or not built; `note` says why), rows, "
            "rows_exact, first_timestamp, last_timestamp, span_seconds, "
            "max_window_seconds, whole_span_fits_one_window, window_start and "
            "window_end (a window that covers every row, end exclusive: pass them "
            "as start_time and end_time), levels [{severity, count}], "
            "timestamp_source and note. Local datasets are counted exactly; the "
            "remote corpus reads file footers only (levels declared, uncounted); "
            "the in-place index reports the window it was built over. Catalog rows "
            "describe the data; they are not a frequency over an investigated scope."
        ),
        kind="catalog",
        catalog=(
            "TIER metadata. No params. One row per dataset, measured on this call: "
            "rows, first_timestamp, last_timestamp, span, max_window_seconds, a "
            "ready window_start/window_end covering every row, and severity levels "
            "(exact counts on local datasets; declared, uncounted on the remote "
            "corpus; for otel_slice the window the index was built over). Call it "
            "FIRST when the user named no time window, so windows and severity "
            "filters come from the data instead of a guess."
        ),
        args_model=None,
        run=_run_list_datasets,
        operation=lambda args: (
            "list every dataset with its rows, time span and severity levels"
        ),
        complete=True,
    ),
    ToolSpec(
        name="count_by_dataset_tool",
        title="Count entries per dataset",
        description=(
            "Count log entries per severity in each WHOLE local dataset, in one "
            "query. Use it for a corpus-wide overview or to compare datasets: every "
            "row is counted, no time window. For a time window or a breakdown by "
            "service, and for the remote corpus (which cannot be counted whole and "
            "is rejected), use count_by_category_tool. Returns kind=aggregate rows "
            "{dataset, severity, count}: measured counts of log entries, not "
            "incidents, with no row cap. An unloaded dataset is refused, never left "
            "out of the totals."
        ),
        kind="aggregate",
        catalog=(
            "TIER index. Counts log entries per severity in each WHOLE local "
            "dataset in one query; params: datasets (optional list, default every "
            "local dataset). No time window: local datasets are counted over every "
            "row in milliseconds. The remote corpus cannot be counted whole and is "
            "rejected. Use for a corpus-wide overview or to compare datasets; the "
            "counts are measured (kind aggregate)."
        ),
        args_model=schemas.CountByDataset,
        run=_run_count_by_dataset,
        operation=lambda args: count_by_dataset_operation(args["datasets"]),
        # Several datasets: named in the filters (the data scope reads them from
        # there), not in the single-dataset envelope field.
        filters=lambda args: {"datasets": args["datasets"]},
        dataset_of=lambda args: None,
        complete=True,
        open_world=False,
    ),
    ToolSpec(
        name="get_trend_tool",
        title="Trend of one value",
        description=(
            "Count one known field value per time bucket, to see how it moves over "
            "a window. Find the exact value with get_data_tool first. Returns "
            "kind=trend rows {bucket, [severity,] count, prev_count, trend} for "
            "EVERY bucket in the window, with count 0 where nothing was logged, so "
            "a gap shows as a gap; `query_stats.notes` names the bucket used and "
            "summarizes consecutive non-zero bucket runs with their totals. "
            "Measured counts of log entries. A window that would need more than 240 "
            "points is refused with a coarser bucket suggested."
        ),
        kind="trend",
        catalog=(
            "TIER index|remote. Counts one known service/entity/event per time "
            "bucket; params: dataset, start_time, end_time, type=(field, exact "
            "value), bucket (second|minute|hour|day|week|month; omit it and the "
            "window picks one), category (severity), group_by ('severity' for one "
            "series per level). Every bucket in the window is returned, 0 where "
            "nothing was logged, so a gap shows as a gap. query_stats.notes also "
            "summarizes consecutive non-zero bucket runs with their measured totals. "
            "Use to see how one known entity's volume moves over time."
        ),
        args_model=schemas.GetTrend,
        run=_run_trend,
        operation=lambda a: trend_operation(
            a["type"][0], a["type"][1], a["dataset"], a["category"], a["group_by"]
        ),
        filters=lambda a: trend_filters(
            a["start_time"], a["end_time"], a["type"][0], a["type"][1],
            a["category"], a["bucket"], a["group_by"],
        ),
        limit_of=lambda a: None,
        complete=True,
    ),
    ToolSpec(
        name="count_by_category_tool",
        title="Count by severity",
        description=(
            "Count log rows per severity in a time window, overall or broken down "
            "by another field. The cheapest aggregate: it reads only the timestamp, "
            "severity and grouping columns, never message text, so use it first to "
            "find where the problem is before paying to search text. Returns "
            "kind=aggregate rows {[<type>,] severity, count} computed in the "
            "database over every matching row: a measured frequency for the stated "
            "window and filters (row counts, not incidents). "
            "`query_stats.approximate` marks a corpus without exact counts."
        ),
        kind="aggregate",
        catalog=(
            "TIER index|remote. CHEAPEST aggregate: reads only the timestamp, "
            "severity and grouping columns, never message text; params: dataset, "
            "start_time, end_time, type (general|service|entity_id|template|event_id), "
            "category, limit. Use this FIRST to find where the problem is before "
            "paying to search text."
        ),
        args_model=schemas.CountByCategory,
        run=_run_count_by_category,
        operation=lambda a: (
            f"count {_COUNT_LABELS.get(a['type'], a['type'])} on {a['dataset']} by severity"
            + (f" for '{a['category']}'" if a["category"] else "")
        ),
        filters=lambda a: {
            "start_time": a["start_time"],
            "end_time": a["end_time"],
            "category": a["category"],
            "group_by": (
                "category" if a["type"] == "general" else f"{a['type']}, category"
            ),
        },
    ),
    ToolSpec(
        name="sweep_chunks_tool",
        title="Sweep a remote corpus",
        description=(
            "Examine a whole remote corpus in graded passes and rank the chunks "
            "that look relevant. LAST RESORT: use it only when the user insists on "
            "evidence from the entire corpus and a bounded query will not do. It "
            "maps every chunk from Parquet footers, profiles them with aggregates "
            "that read no message text, samples the most promising and ranks them; "
            "with max_promotions above zero it also reads message text for that "
            "many top chunks (about eleven minutes each) and returns verbatim "
            "exemplars. Always call it first with confirm_cost=false to get a cost "
            "estimate, show it to the user, and call again with confirm_cost=true "
            "only once they agree. Returns kind=search rows: one {coverage, "
            "progress, cursor, notes, trace, sampling} row, then {ranked_chunk} and "
            "{digest} rows. Report `coverage`: a profiled chunk was counted, not "
            "read, so a sweep can miss evidence and the answer must not imply "
            "otherwise. `trace` lists every stage and the score behind each "
            "decision; `sampling` gives each sample's examined fraction and a "
            "likely RANGE for its match rate (about 0.3% of a chunk, in a handful "
            "of windows, is a cheap guess): quote the range, not the point "
            "estimate, and say when the windows disagreed. The ranking is not a "
            "frequency."
        ),
        kind="search",
        catalog=(
            "TIER sweep. LAST RESORT, and only when the user insists on evidence "
            "from the WHOLE corpus and a bounded query will not do; params: "
            "dataset, question, max_promotions (0-4), start_at, confirm_cost. "
            "Examines all 1,624 chunks in graded passes -- footers, then aggregates "
            "that read no message text, then stratified samples -- and ranks them. "
            "max_promotions>0 reads message text for that many top chunks at about "
            "ELEVEN MINUTES EACH. ALWAYS call with confirm_cost=false first, show "
            "the user the estimate, then call again with confirm_cost=true. Report "
            "the returned coverage: a profiled chunk was counted, not read. The "
            "result also carries a `trace` of every step and the score behind each "
            "decision -- cite it when explaining why a particular chunk was read. "
            "`sampling` gives each sample's examined fraction and a likely RANGE "
            "for its match rate: quote the range rather than the single number, "
            "because a sample of ~0.3% of a chunk in a few windows is a cheap "
            "guess, not a measurement."
        ),
        args_model=schemas.SweepChunks,
        run=_run_sweep,
        operation=_sweep_operation,
        filters=_sweep_filters,
        limit_of=lambda a: None,
        complete=True,
        reports_progress=True,
        read_only=False,
        sweep=True,
        timeout=None,
    ),
    ToolSpec(
        name="investigate_chunk_tool",
        title="Read one chunk",
        description=(
            "Read one named chunk's message text and return counts, templates and "
            "verbatim lines. Use it on a chunk a sweep ranked highly, when the "
            "ranking alone is not enough and the investigation needs quotable "
            "lines. It indexes the chunk, summarises it, then drops it, so the cost "
            "(about eleven minutes) is paid once and the disk is returned. Returns "
            "kind=search: one row with the chunk's counts, templates, verbatim "
            "lines and `coverage`. Reading one chunk says nothing about the rest of "
            "the corpus; carry the returned coverage into the answer."
        ),
        kind="search",
        catalog=(
            "TIER sweep. Reads one named chunk's message text and returns counts, "
            "templates and verbatim lines; params: dataset, chunk_id, question. "
            "Use on a chunk a sweep ranked highly when you need quotable evidence. "
            "Costs about eleven minutes. Reading one chunk says nothing about the "
            "rest of the corpus, and the returned coverage says so."
        ),
        args_model=schemas.InvestigateChunk,
        run=_run_investigate,
        operation=lambda a: f"read message text for chunk {a['chunk_id']}",
        filters=lambda a: {"chunk_id": a["chunk_id"], "question": a["question"]},
        limit_of=lambda a: None,
        complete=True,
        read_only=False,
        sweep=True,
        timeout=None,
    ),
    ToolSpec(
        name="search_logs_tool",
        title="Search log messages",
        description=(
            "Search log message text on an indexed corpus and return the "
            "best-matching example lines, optionally for one severity and within a "
            "time window. Reads the inverted index, so it never scans the remote "
            "files; on the OTel corpus use the indexed slice (otel_slice), because "
            "the raw remote tier has no index and is refused. Use it to read actual "
            "log lines once the problem is located; locate it first with "
            "count_by_category_tool, and put a severity in `category`, not in the "
            "words. Returns kind=search: at most `limit` example rows {record_id, "
            "ts, severity, service, entity_id, message, relevance, and event_id "
            "where the corpus has one}, plus `total_matches` (every row matching "
            "the same words, severity and window; null for a semantic search), an "
            "exact `truncated` when total_matches is known, and the executed "
            "query. Examples never measure frequency: count with "
            "count_by_category_tool. Zero rows means nothing matched in that scope, "
            "not that the event never happened."
        ),
        kind="search",
        catalog=(
            "TIER index ONLY. Searches message text and ranks by relevance; "
            "params: dataset, query_text, category, limit, start_time/end_time "
            "(optional, both or neither). Requires an indexed dataset -- it is "
            "refused on the raw remote corpus, which has no index. Returns "
            "example rows plus total_matches: every row matching the same "
            "words, severity and window, so the answer can say 'n shown of "
            "total_matches'. Use to read the actual log lines once a window is "
            "narrowed."
        ),
        args_model=schemas.SearchLogs,
        run=_run_search,
        operation=lambda a: search_operation(
            a["query_text"], a["dataset"], a["category"], a["start_time"], a["end_time"]
        ),
        filters=lambda a: search_filters(
            a["query_text"], a["category"], a["start_time"], a["end_time"]
        ),
    ),
    ToolSpec(
        name="top_in_category_by_service_tool",
        title="Rank values by volume",
        description=(
            "Rank a field's values (default: the emitting service) by log volume in "
            "a time window, optionally for one severity. Use it to locate the "
            "noisiest components before searching text. Returns kind=aggregate "
            "rows {<field>, count}, the top `limit` by count, computed over every "
            "row in the window: measured counts of log entries, not incidents. "
            "`truncated` means more values exist."
        ),
        kind="aggregate",
        catalog=(
            "TIER index|remote. Ranks a field's values by log volume in a window; "
            "params: dataset, start_time, end_time, field, category, limit. "
            "Use to locate the noisiest services."
        ),
        args_model=schemas.TopInCategoryByService,
        run=_run_top,
        operation=_rank_operation,
        filters=lambda a: {
            "start_time": a["start_time"],
            "end_time": a["end_time"],
            "category": a["category"],
            "group_by": a["field"],
        },
    ),
    ToolSpec(
        name="compare_periods_tool",
        title="Compare two periods",
        description=(
            "Compare per-value row counts between a baseline window and a recent "
            "window, to see what changed. It scans both windows, so it costs "
            "roughly twice a single count. Returns kind=aggregate rows {<field>, "
            "severity, baseline_count, recent_count, volume_change, "
            "percentage_change}, ordered by volume_change: measured counts for the "
            "top `limit` values."
        ),
        kind="aggregate",
        catalog=(
            "TIER index|remote. Compares counts between a baseline and a recent "
            "window; params: dataset, baseline_start/end, recent_start/end, field, "
            "limit. Scans BOTH windows, so it costs roughly twice a single count. "
            "Use to detect what changed."
        ),
        args_model=schemas.ComparePeriods,
        run=_run_compare,
        operation=lambda a: (
            f"compare {a['dataset']} log volume across two time windows"
        ),
        filters=lambda a: {
            "baseline_start": a["baseline_start"],
            "baseline_end": a["baseline_end"],
            "recent_start": a["recent_start"],
            "recent_end": a["recent_end"],
            "group_by": a["field"],
        },
    ),
    ToolSpec(
        name="get_data_tool",
        title="List common field values",
        description=(
            "List the most common values of a field in a time window, ranked by row "
            "count. Use it to discover a valid filter value (an exact service, "
            "template or entity id) before requesting a trend or filtering. Fields "
            "whose distinct set is effectively unbounded, such as trace ids on the "
            "OTel corpus, are refused with an alternative. Returns kind=catalog "
            "rows {value, count}: for discovery, not a frequency analysis."
        ),
        kind="catalog",
        catalog=(
            "TIER index|remote. Lists a field's most common values with counts; "
            "params: dataset, start_time, end_time, type, limit. Ranked by volume, "
            "so the first rows are the significant ones. Use to discover a valid "
            "filter value before requesting a trend. Refused for fields whose "
            "distinct set is effectively unbounded, such as trace ids."
        ),
        args_model=schemas.GetTools,
        run=_run_values,
        operation=lambda a: f"list the most common {a['type']} values on {a['dataset']}",
        filters=lambda a: {
            "start_time": a["start_time"],
            "end_time": a["end_time"],
            "field": a["type"],
        },
    ),
    ToolSpec(
        name="get_log_by_line_id_tool",
        title="Fetch a log line by id",
        description=(
            "Fetch one complete log row by its primary-key id. Use it to resolve a "
            "'Line ID: n (dataset)' reference (what the chat box inserts for a "
            "picked log line) into the whole record. Ids are unique only within one "
            "corpus, so pass both the id and the dataset. Only corpora that have a "
            "primary key support it; the raw OTel corpus has none and refuses with "
            "a pointer to those that do. Returns kind=lookup: zero or one full row "
            "(record_id plus the canonical fields); zero rows means no such id in "
            "that dataset."
        ),
        kind="lookup",
        catalog=(
            "TIER index. Fetches one complete row by primary key; params: dataset, "
            "line_id. Only datasets that declare a record id support it -- the raw "
            "remote corpus has no primary key and refuses this."
        ),
        args_model=schemas.GetLogByLineId,
        run=_run_line_id,
        operation=lambda a: f"fetch {a['dataset']} row with id {a['line_id']}",
        filters=lambda a: {"line_id": a["line_id"]},
        limit_of=lambda a: None,
        complete=True,
        open_world=False,
    ),
    ToolSpec(
        name="get_nearby_events_tool",
        title="Events around a log line",
        description=(
            "Return the log lines just before and after one anchor line, for the "
            "engineer's before/after timeline. Use it on a search hit's record_id "
            "or a 'Line ID: n (dataset)' reference, after search_logs_tool or "
            "get_log_by_line_id_tool found the line. Neighbours follow record-id "
            "(file) order, not a time window; same_entity narrows them to the "
            "anchor's process (OpenStack) or node (BGL), same_request to its "
            "request id (OpenStack only; refused elsewhere). Datasets without a "
            "record id (the OTel tiers) are refused. Returns kind=context rows "
            "ordered by record_id: record_id, ts, severity, service, entity_id, "
            "request_id (OpenStack), message, template and offset (negative "
            "before the anchor, 0 for the anchor, positive after); with "
            "same_request, `query_stats.notes` also summarizes the whole request "
            "(rows, first and last timestamp, components). An unknown line_id, or "
            "an anchor with no value for the flagged field, is a success with "
            "empty or anchor-only data and a note. Context rows are examples, "
            "never a frequency: count with count_by_category_tool."
        ),
        kind="context",
        catalog=(
            "TIER index. Returns the log entries just before and after one anchor "
            "line, in file order; params: dataset (openstack or bgl), line_id (a "
            "search hit's record_id), before, after (0-50, default 5), "
            "same_entity (only the anchor's process or node), same_request (only "
            "the anchor's request id; OpenStack only). Rows are examples (kind "
            "context) with an offset from the anchor, never a frequency. Use for "
            "the engineer's before/after timeline."
        ),
        args_model=schemas.GetNearbyEvents,
        run=_run_nearby,
        operation=lambda a: nearby_operation(
            a["line_id"], a["dataset"], a["before"], a["after"],
            a["same_entity"], a["same_request"],
        ),
        filters=lambda a: {
            "line_id": a["line_id"],
            "before": a["before"],
            "after": a["after"],
            "same_entity": a["same_entity"],
            "same_request": a["same_request"],
        },
        limit_of=lambda a: None,
        # The row count is what was asked for, not a cap that cut anything off.
        complete=True,
        open_world=False,
    ),
    ToolSpec(
        name="count_by_team_tool",
        title="Count entries per team",
        description=(
            "Count log entries per owning team and severity in the local "
            "datasets, joining each row's service (Component) to the "
            "team_ownership table by corpus and component. Use it to group "
            "recurring problems by team, or to say which team owns most of the "
            "errors; for a breakdown by service use count_by_category_tool. Omit "
            "the window to count every row. Services with no owner count as team "
            "'unowned', so team totals equal the dataset totals. Returns "
            "kind=aggregate rows {dataset, team, severity, count} ordered by "
            "count, computed in the database over every matching row: measured "
            "log entries, not incidents, with no row cap. The remote corpus, an "
            "unloaded dataset and a missing ownership table are refused with a "
            "suggestion; a severity absent from some datasets counts only the "
            "others and says so in `query_stats.notes`."
        ),
        kind="aggregate",
        catalog=(
            "TIER index. Counts log entries per owning team and severity over local "
            "datasets; params: datasets, start_time, end_time (optional paired "
            "window), category. Returns measured kind=aggregate rows with "
            "dataset, team, severity and count, ordered by count. It joins the "
            "ownership mapping and counts unowned services as team 'unowned'. "
            "Use to group recurring problems by team; counts are log entries, not "
            "incidents."
        ),
        args_model=schemas.CountByTeam,
        run=_run_count_by_team,
        operation=lambda a: count_by_team_operation(
            a["datasets"], a["start_time"], a["end_time"], a["category"]
        ),
        filters=lambda a: {
            "datasets": a["datasets"],
            "start_time": a["start_time"],
            "end_time": a["end_time"],
            "category": a["category"],
        },
        # Several datasets: named in the filters, as for count_by_dataset_tool.
        dataset_of=lambda a: None,
        limit_of=lambda a: None,
        complete=True,
        open_world=False,
    ),
    ToolSpec(
        name="rank_priorities_tool",
        title="Rank problems across datasets",
        description=(
            "Rank the biggest problems in each dataset from measured counts in two "
            "adjacent time windows, on the server. Use it instead of ranking "
            "counts yourself: it takes no numbers from the caller, reads every "
            "count from SQL and scores with the pipeline's own formula, so the "
            "order is checkable. Returns kind=derived rows, one per ranked value, "
            "each carrying dataset, rank, the grouped value (template, service or "
            "team), non_info_rows, error_rows, volume_share_pct, error_share_pct, "
            "trend_up, breadth and score -- at most `limit` rows per dataset "
            "(default 10), while `total_matches` says how many values were ranked "
            "in all. A row is a ranking, not a frequency: quote its "
            "non_info_rows and the queries in `query`, never the score alone. The "
            "two windows must not overlap, because a value's count is the sum of "
            "its two window counts and the trend compares them. A remote dataset "
            "is ranked only with group_by='service' or 'template', in windows of "
            "at most 300 seconds, and its counts are approximate; group_by='team' "
            "is local only. Empty data means no non-informational rows in either "
            "window for that dataset; `query_stats.notes` names the formula and "
            "the terms used, and flags any value dropped because a window hit its "
            "group cap."
        ),
        kind="derived",
        catalog=(
            "TIER server. Ranks problems per dataset from two non-overlapping "
            "windows; params: datasets, group_by (template|service|team), "
            "baseline_start/end, recent_start/end, limit (1-50). Scores measured "
            "SQL counts with the pipeline formula and returns kind=derived rows "
            "plus the formula, terms used and queries. Offered on the MCP server "
            "only: the pipeline adds its own ranking in answer_node."
        ),
        args_model=schemas.RankPriorities,
        run=_run_rank_priorities,
        operation=rank_priorities_operation,
        filters=rank_priorities_filters,
        # Several datasets, each ranked on its own: named in the filters.
        dataset_of=lambda a: None,
        limit_of=lambda a: a["limit"],
        open_world=True,
        pipeline=False,
    ),
)

BY_NAME: dict[str, ToolSpec] = {spec.name: spec for spec in SPECS}


def _sweep_last(specs: list[ToolSpec]) -> list[ToolSpec]:
    """Available specs in table order, with the sweep pair last.

    The sweep tools run last on purpose: ``tool_progress`` only reaches the UI
    from depth <= 1, and a sweep that reports nothing while it runs for minutes
    is worse than no sweep.
    """
    live = [spec for spec in specs if spec.available]
    return [s for s in live if not s.sweep] + [s for s in live if s.sweep]


def agent_specs() -> list[ToolSpec]:
    """The tools the pipeline's agent offers: available and pipeline, sweep last."""
    return _sweep_last([spec for spec in SPECS if spec.pipeline])


def server_specs() -> list[ToolSpec]:
    """Every tool the MCP server offers: every available spec, sweep last.

    Unlike :func:`agent_specs`, this includes server-only tools
    (``pipeline=False``), which the pipeline's own agent never sees.
    """
    return _sweep_last(list(SPECS))


def pipeline_specs() -> list[ToolSpec]:
    """Every spec the pipeline may offer, planned ones included.

    ``tools.__all__`` and the classifiers' ``TOOL_CATALOG`` are built from this
    (planned tools are declared there with a "NOT AVAILABLE YET" line), while
    :func:`agent_specs` is what a transport actually generates.
    """
    return [spec for spec in SPECS if spec.pipeline]
