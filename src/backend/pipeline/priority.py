"""Deterministic priority score computed from database aggregates.

The manager ranks problems and the team lead picks what to investigate first.
Ranking is arithmetic over measured counts, so it lives in code, never in a
model (Jev and LLMs are unreliable at counting and comparing numbers):

    score = 0.4*volume_share + 0.3*error_share + 0.2*trend_up + 0.1*breadth

    volume_share  item's non-INFO rows / all non-INFO rows across items
    error_share   item's ERROR-or-worse rows / item's non-INFO rows
    trend_up      clamp((recent - baseline) / max(baseline, 1), 0, 1), from a
                  period comparison, or second half vs first half of a trend
    breadth       distinct components the item appears in / all components

A term that no result measures is left out and the remaining weights are
renormalized; an item lacking a term that others have scores 0 on it and shows
"n/a". The output is a derived result record (``kind="derived"``) that renders
as a table and cites the R-ids it was computed from, so the ranking is checkable.

When new tools land (team counts, nearby events), nothing here changes as long
as their aggregate rows carry ``count`` plus a group column (``team``,
``service``, ``event_id`` ...) and, ideally, ``category``.
"""

from collections import defaultdict
from collections.abc import Callable, Sequence
from typing import Any

from backend.pipeline.tools.envelope import MEASURED_KINDS

WEIGHTS = {"volume_share": 0.4, "error_share": 0.3, "trend_up": 0.2, "breadth": 0.1}
FORMULA = (
    "score = 0.4*volume_share + 0.3*error_share + 0.2*trend_up + 0.1*breadth "
    "(unmeasured terms dropped, weights renormalized)"
)
DERIVED_TOOL_NAME = "priority_score"

# Group columns that identify a "problem", in preference order. The dataset
# registry (``backend.db.datasets``) returns canonical names (``service``,
# ``entity_id``, ``severity``, ``template``); ``event_id`` exists only where a
# corpus has one, and ``component``/``pid``/``category`` are the pre-registry
# OpenStack names, kept so stored history still ranks.
DIMENSIONS = ("event_id", "template", "service", "component", "team", "entity_id", "pid")
SEVERITY_COLUMNS = ("severity", "category")
COMPONENT_COLUMNS = ("service", "component")
# Compared upper-cased. Every informational value a registered corpus declares
# in ``severity_values`` must be listed (OTel uses both ``info`` and
# ``information``); ``test_priority_levels_cover_every_registered_dataset`` checks it.
INFO_LEVELS = frozenset({"INFO", "INFORMATION", "DEBUG", "TRACE"})
SEVERE_LEVELS = frozenset({"ERROR", "FATAL", "SEVERE", "FAILURE", "CRITICAL"})
MAX_ROWS = 10


def _measured(records: Sequence[dict]) -> list[dict]:
    return [
        r for r in records
        if isinstance(r, dict) and r.get("kind") in MEASURED_KINDS and r.get("rows")
    ]


def _pick_dimension(records: Sequence[dict]) -> str | None:
    for dimension in DIMENSIONS:
        for record in records:
            row = record["rows"][0]
            if dimension in row and "count" in row:
                return dimension
    return None


def _category(row: dict, record: dict) -> str | None:
    """The row's severity: its own severity column, else the call's filter.

    Upper-cased because corpora differ in case (OTel ``error`` vs BGL ``ERROR``).
    """
    value = next((row[c] for c in SEVERITY_COLUMNS if row.get(c)), None)
    if value is None:
        value = (record.get("filters") or {}).get("category")
    return str(value).upper() if value else None


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def compute_priorities(
    records: Sequence[dict], team_for: Callable[[str | None], str] | None = None
) -> dict[str, Any] | None:
    """Rank problems from aggregate/trend records; ``None`` without counts."""
    measured = _measured(records)
    dimension = _pick_dimension(measured)
    if dimension is None:
        return None

    # (value, category) -> max count, so overlapping queries are not summed.
    counts: dict[tuple[str, str | None], int] = {}
    components: dict[str, set[str]] = defaultdict(set)
    all_components: set[str] = set()
    sources: list[str] = []
    for record in measured:
        rows = record["rows"]
        if dimension in rows[0] and "count" in rows[0]:
            sources.append(record["id"])
            for row in rows:
                key = (str(row[dimension]), _category(row, record))
                counts[key] = max(counts.get(key, 0), int(row["count"] or 0))
                for column in COMPONENT_COLUMNS:
                    if column != dimension and row.get(column):
                        components[str(row[dimension])].add(str(row[column]))
                        all_components.add(str(row[column]))

    issue: dict[str, int] = defaultdict(int)
    severe: dict[str, int] = defaultdict(int)
    has_category = any(category is not None for _, category in counts)
    for (value, category), count in counts.items():
        if category in INFO_LEVELS:
            continue
        issue[value] += count
        if category in SEVERE_LEVELS:
            severe[value] += count
    total = sum(issue.values())
    if not total:
        return None

    trend, trend_sources = _trend_by_value(measured, dimension)

    terms = {
        "volume_share": True,
        "error_share": has_category,
        "trend_up": bool(trend),
        "breadth": bool(all_components),
    }
    used = [name for name, ok in terms.items() if ok]
    weight_sum = sum(WEIGHTS[name] for name in used)

    rows = []
    for value, count in issue.items():
        if not count:
            # A value whose non-INFO rows all count 0 has nothing to rank (and
            # would divide by zero in its error share).
            continue
        parts = {
            "volume_share": count / total,
            "error_share": severe[value] / count if has_category else None,
            "trend_up": trend.get(value) if trend else None,
            "breadth": (
                len(components[value]) / len(all_components) if all_components else None
            ),
        }
        score = sum(WEIGHTS[name] * (parts[name] or 0.0) for name in used) / weight_sum
        row = {
            dimension: value,
            "non_info_rows": count,
            "error_rows": severe[value] if has_category else None,
            "volume_share_pct": round(100 * parts["volume_share"], 1),
            "error_share_pct": (
                round(100 * parts["error_share"], 1) if parts["error_share"] is not None else None
            ),
            "trend_up": round(parts["trend_up"], 2) if parts["trend_up"] is not None else "n/a",
            "breadth": round(parts["breadth"], 2) if parts["breadth"] is not None else "n/a",
            "score": round(score, 3),
        }
        if team_for and dimension in COMPONENT_COLUMNS:
            row["team"] = team_for(value)
        rows.append(row)

    rows.sort(key=lambda r: (-r["score"], -r["non_info_rows"], str(r[dimension])))
    for rank, row in enumerate(rows, start=1):
        row["rank"] = rank
    ordered = [{"rank": r.pop("rank"), **r} for r in rows[:MAX_ROWS]]
    return {
        "dimension": dimension,
        "rows": ordered,
        "terms_used": used,
        "formula": FORMULA,
        "source_ids": sorted(set(sources) | set(trend_sources), key=lambda i: (len(i), i)),
        "total_ranked": len(rows),
    }


def _trend_by_value(
    records: Sequence[dict], dimension: str
) -> tuple[dict[str, float], list[str]]:
    """``(value -> trend_up, source ids)`` from comparisons or per-minute trends."""
    baseline: dict[str, int] = defaultdict(int)
    recent: dict[str, int] = defaultdict(int)
    trend_sources: list[str] = []
    for record in records:
        rows = record["rows"]
        first = rows[0]
        if dimension in first and "baseline_count" in first and "recent_count" in first:
            trend_sources.append(record["id"])
            for row in rows:
                if _category(row, record) in INFO_LEVELS:
                    continue
                baseline[str(row[dimension])] += int(row["baseline_count"] or 0)
                recent[str(row[dimension])] += int(row["recent_count"] or 0)
        elif record.get("kind") == "trend" and "count" in first:
            value = (record.get("filters") or {}).get(dimension)
            if value is None or _category(first, record) in INFO_LEVELS:
                continue
            series = [int(row.get("count") or 0) for row in rows]
            half = len(series) // 2
            if half == 0:
                continue
            trend_sources.append(record["id"])
            # Two equal-length halves ending at the newest bucket; on an odd
            # length the OLDEST bucket is dropped, never the most recent one.
            baseline[str(value)] += sum(series[-2 * half:-half])
            recent[str(value)] += sum(series[-half:])
    result = {
        value: _clamp((recent[value] - baseline[value]) / max(baseline[value], 1))
        for value in set(baseline) | set(recent)
    }
    return result, trend_sources


def priority_record(
    records: Sequence[dict], team_for: Callable[[str | None], str] | None = None
) -> dict[str, Any] | None:
    """The derived ``R{n}`` record for the ranking, or ``None`` without counts."""
    base = [r for r in records if r.get("tool_name") != DERIVED_TOOL_NAME]
    ranking = compute_priorities(base, team_for)
    if ranking is None:
        return None
    sources = ranking["source_ids"]
    return {
        "id": f"R{len(base) + 1}",
        "tool_name": DERIVED_TOOL_NAME,
        "arguments": {"dimension": ranking["dimension"], "from": sources},
        "row_count": len(ranking["rows"]),
        "rows": ranking["rows"],
        "rows_capped": ranking["total_ranked"] > len(ranking["rows"]),
        "kind": "derived",
        "dataset": None,
        "query": None,
        "filters": {"terms_used": ranking["terms_used"]},
        "limit": MAX_ROWS,
        "returned": len(ranking["rows"]),
        "total_matches": ranking["total_ranked"],
        "truncated": ranking["total_ranked"] > len(ranking["rows"]),
        "formula": ranking["formula"],
        "source_ids": sources,
    }


def with_priority(
    records: Sequence[dict], team_for: Callable[[str | None], str] | None = None
) -> list[dict]:
    """Records with exactly one fresh derived priority record (idempotent)."""
    base = [r for r in records if r.get("tool_name") != DERIVED_TOOL_NAME]
    derived = priority_record(base, team_for)
    return [*base, derived] if derived else base
