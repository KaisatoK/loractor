"""Investigation cost accounting, computed off-model.

Requirement: an answer must report how much data was actually queried, whether
the result is still useful, and the observed time and cost.

None of that is asked of the model. Each tool result already carries a
``query_stats`` record produced by the query layer from Parquet footer metadata
and a wall clock, so the totals are *derived* here and rendered directly. A
number the model never sees is a number the model cannot get wrong.

``usefulness`` is the honesty half. A query that read six million rows and
returned six grouped rows is useful; one that read the same six million and
returned nothing is a true negative worth stating as such, not silently
dropping.
"""

from __future__ import annotations

from backend.db.stats import QueryStats, merge


def _stats_from(record: dict) -> QueryStats | None:
    """Rebuild a QueryStats from a tool result's serialized ``query_stats``."""
    raw = record.get("query_stats")
    if not isinstance(raw, dict):
        return None

    allowed = {f for f in QueryStats.__dataclass_fields__}
    return QueryStats(**{k: v for k, v in raw.items() if k in allowed})


def collect_stats(tool_results: list[dict] | None) -> list[tuple[str, QueryStats]]:
    """``(result_id, stats)`` for every tool result that reported cost."""
    collected: list[tuple[str, QueryStats]] = []
    for record in tool_results or []:
        stats = _stats_from(record)
        if stats is not None:
            collected.append((str(record.get("id") or "?"), stats))
    return collected


def investigation_cost(tool_results: list[dict] | None) -> dict:
    """Per-query and total cost for one investigation.

    Returns an empty-but-valid shape when no tool reported stats, so callers
    never have to branch on presence.
    """
    collected = collect_stats(tool_results)
    if not collected:
        return {
            "queries": [],
            "total": None,
            "approximate_count": 0,
            "datasets": [],
            "note": "",
        }

    per_query = []
    for result_id, stats in collected:
        per_query.append(
            {
                "id": result_id,
                "dataset": stats.dataset,
                "rows_returned": stats.row_count,
                "rows_scanned": stats.est_rows_scanned,
                "bytes_read": stats.est_bytes_read,
                "elapsed_ms": stats.elapsed_ms,
                "approximate": stats.approximate,
                "files_read": stats.files_read,
                "files_considered": stats.files_considered,
                "row_groups_read": stats.row_groups_read,
                "pruning_ratio": round(stats.pruning_ratio, 4),
                "useful": stats.row_count > 0,
                "summary": stats.summary(),
            }
        )

    total = merge([stats for _, stats in collected])
    datasets = [d for d in total.dataset.split(",") if d]

    return {
        "queries": per_query,
        "total": {
            "queries": len(per_query),
            "rows_scanned": total.est_rows_scanned,
            "bytes_read": total.est_bytes_read,
            "elapsed_ms": total.elapsed_ms,
            "rows_returned": total.row_count,
            "approximate": total.approximate,
            "files_considered": total.files_considered,
            "files_read": total.files_read,
        },
        "approximate_count": sum(1 for q in per_query if q["approximate"]),
        "datasets": datasets,
        # Timing broken out, so a caller measuring agent performance does not
        # have to re-derive it from the per-query list.
        "timing": {
            "total_ms": total.elapsed_ms,
            "query_count": len(per_query),
            "slowest_ms": max((q["elapsed_ms"] for q in per_query), default=0),
            "slowest_id": max(
                per_query, key=lambda q: q["elapsed_ms"], default={"id": None}
            ).get("id"),
            "mean_ms": round(total.elapsed_ms / max(len(per_query), 1), 1),
            "rows_per_second": round(
                total.est_rows_scanned / (total.elapsed_ms / 1000), 1
            )
            if total.elapsed_ms and total.est_rows_scanned
            else 0.0,
        },
        "note": format_cost_note(per_query, total),
    }


def format_cost_note(per_query: list[dict], total: QueryStats) -> str:
    """One deterministic sentence stating what the investigation read.

    Appended to the answer text, so it survives into the persisted message and
    is not dependent on the UI being open.
    """
    if not per_query:
        return ""

    # Timing leads, because it is the figure a reader uses to judge whether the
    # investigation was efficient, and an average hides the shape of the work:
    # eight fast aggregates and one slow text read average to something that
    # describes neither.
    parts: list[str] = [
        f"{len(per_query)} queries in {total.elapsed_ms / 1000:.2f}s"
    ]

    slowest = max(per_query, key=lambda q: q["elapsed_ms"])
    if len(per_query) > 1 and slowest["elapsed_ms"] > 0:
        share = 100.0 * slowest["elapsed_ms"] / max(total.elapsed_ms, 1)
        parts.append(
            f"slowest {slowest['id']} at {slowest['elapsed_ms'] / 1000:.2f}s "
            f"({share:.0f}% of the total)"
        )

    if total.files_considered:
        parts.append(f"read {total.files_read} of {total.files_considered} files")
    if total.est_rows_scanned:
        parts.append(f"~{total.est_rows_scanned:,} rows scanned")
    if total.est_bytes_read:
        parts.append(f"~{_human_bytes(total.est_bytes_read)}")

    if total.est_rows_scanned and total.elapsed_ms:
        rate = total.est_rows_scanned / (total.elapsed_ms / 1000)
        parts.append(f"~{rate:,.0f} rows/s")

    approximate = sum(1 for q in per_query if q["approximate"])
    if approximate:
        parts.append(f"{approximate} of {len(per_query)} results approximate")

    empty = sum(1 for q in per_query if not q["useful"])
    if empty:
        parts.append(f"{empty} returned no rows")

    return "Query cost: " + ", ".join(parts) + "."


def _human_bytes(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(size) < 1024.0 or unit == "TiB":
            return f"{int(size)} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} TiB"  # pragma: no cover
