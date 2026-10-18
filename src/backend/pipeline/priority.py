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

A value's count comes from its ``count`` row, or, for a period comparison, from
``baseline_count + recent_count`` -- the same rule ``pipeline/ranking.py``
applies. When a run brings the scope's two halves as two separate windowed counts
(baseline and recent) rather than one comparison, they are paired automatically,
so every candidate gets both window counts and ``trend_up`` is measured for all of
them (a period comparison is ordered by ``volume_change`` and truncates, so it
leaves large, flat values with no trend). The ranked dimension is the first
count-bearing one in :data:`DIMENSIONS` order, except that a dimension which
carries a trend -- a comparison, or such a pair -- wins.

Each dataset is ranked on its own: shares use that dataset's totals and every row
carries its ``dataset``, so a short remote window cannot outrank a long local
pattern on raw volume. ``approximate_datasets`` lists the datasets whose sources
were approximate (a corpus without exact counts), so the ranking can be marked
rather than presented as a measurement.

When new tools land (team counts), nothing here changes as long as their
aggregate rows carry ``count`` plus a group column (``team``, ``service``,
``event_id`` ...) and, ideally, ``category``. Nearby events are ``context``
rows (examples), so they never enter the score.
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


def _count_of(row: dict) -> int | None:
    """A row's row count: its ``count``, else a comparison's ``baseline + recent``.

    A period comparison (``compare_periods_tool``) carries the two window counts
    rather than a single ``count``. Summing them is the same rule
    ``pipeline/ranking.py`` applies, and it means the derived ranking can be built
    from a comparison alone -- which is the *better* source, because those rows
    also carry the trend. Returns ``None`` when the row has neither shape.
    """
    if "count" in row:
        return int(row["count"] or 0)
    if "baseline_count" in row and "recent_count" in row:
        return int(row["baseline_count"] or 0) + int(row["recent_count"] or 0)
    return None


def _count_dimensions(records: Sequence[dict]) -> list[str]:
    """Count-bearing dimensions, in :data:`DIMENSIONS` preference order."""
    return [
        dimension
        for dimension in DIMENSIONS
        if any(
            dimension in r["rows"][0] and _count_of(r["rows"][0]) is not None
            for r in records
        )
    ]


def _window_bounds(record: dict) -> tuple[str, str] | None:
    """The ``(start, end)`` a count record's filters state, or ``None``.

    Only the counting tools state ``start_time``/``end_time``; a
    ``compare_periods_tool`` record uses ``baseline_start``/``recent_start``
    instead, so it is never mistaken for one half of a pair.
    """
    filters = record.get("filters") or {}
    start, end = filters.get("start_time"), filters.get("end_time")
    return (str(start), str(end)) if start and end else None


def _paired_rows(baseline: dict, recent: dict, dimension: str) -> list[dict]:
    """``baseline_count``/``recent_count`` rows for two windowed counts.

    A value seen in only one window counts 0 there when that window's result was
    complete; if that result was truncated the value is dropped, because its
    count there is unknown -- the same trust rule ``pipeline/ranking.py`` applies.
    """
    baseline_values = {str(row[dimension]) for row in baseline["rows"]}
    recent_values = {str(row[dimension]) for row in recent["rows"]}
    keys = {
        (str(row[dimension]), _category(row, record))
        for record in (baseline, recent)
        for row in record["rows"]
    }

    def total(record: dict, value: str, severity: str | None) -> int:
        return sum(
            _count_of(row) or 0
            for row in record["rows"]
            if str(row[dimension]) == value and _category(row, record) == severity
        )

    rows = []
    for value, severity in sorted(keys, key=lambda key: (key[0], key[1] or "")):
        if value not in baseline_values and baseline.get("truncated"):
            continue
        if value not in recent_values and recent.get("truncated"):
            continue
        rows.append({
            dimension: value,
            "severity": severity,
            "baseline_count": total(baseline, value, severity),
            "recent_count": total(recent, value, severity),
        })
    return rows


def _paired_comparisons(records: Sequence[dict], dimension: str) -> list[dict]:
    """One comparison record per (dataset, filters) that has exactly two windows.

    A run may bring the two halves of the scope as two ``count_by_category_tool``
    calls instead of one ``compare_periods_tool`` call. Pairing the halves and
    summing the windows gives every candidate both window counts, so ``trend_up``
    is measured for values a period comparison would have dropped: that tool is
    ordered by ``volume_change``, so the biggest *movers* win and a large, flat
    value gets no trend at all.

    The synthesized record carries both source ids in ``source_ids``, so the
    derived ranking cites both halves. When a real comparison already covers the
    dimension nothing is synthesized, so the two are never summed.
    """
    if any(
        dimension in r["rows"][0]
        and "baseline_count" in r["rows"][0]
        and "recent_count" in r["rows"][0]
        for r in records
    ):
        return []

    groups: dict[tuple, dict[tuple[str, str], dict]] = {}
    for record in records:
        bounds = _window_bounds(record)
        if bounds is None or dimension not in record["rows"][0]:
            continue
        if _count_of(record["rows"][0]) is None:
            continue
        filters = record.get("filters") or {}
        key = (
            record.get("dataset"),
            tuple(sorted(
                (name, str(value)) for name, value in filters.items()
                if name not in ("start_time", "end_time") and value is not None
            )),
        )
        groups.setdefault(key, {}).setdefault(bounds, record)

    comparisons: list[dict] = []
    for windows in groups.values():
        if len(windows) != 2:
            continue
        (base_start, base_end), baseline = min(
            windows.items(), key=lambda item: (item[0][0], item[0][1])
        )
        (rec_start, _), recent = max(
            windows.items(), key=lambda item: (item[0][0], item[0][1])
        )
        if rec_start < base_end:
            # Overlapping windows are halves of nothing; summing them would
            # double-count the overlap.
            continue
        rows = _paired_rows(baseline, recent, dimension)
        if not rows:
            continue
        comparisons.append({
            "id": f"{baseline['id']}+{recent['id']}",
            "source_ids": [baseline["id"], recent["id"]],
            # The two halves are replaced by this comparison, so a value dropped
            # for being missing from a truncated window is not re-admitted by the
            # counts loop with an understated total.
            "consumed_ids": [baseline["id"], recent["id"]],
            "kind": "aggregate",
            "dataset": baseline.get("dataset"),
            "filters": {},
            "rows": rows,
        })
    return comparisons


def _pick_dimension(records: Sequence[dict]) -> str | None:
    """The dimension to rank.

    Preference order decides among count-bearing dimensions, except that one which
    also carries a trend wins: a period comparison, or two windowed counts of the
    same dimension, makes ``trend_up`` measurable, and a ranking with a trend is
    more useful than one without. (A manager run brings template counts *and* a
    service comparison; before this rule the count-only template dimension won and
    the ranking had no trend, so the answer ranked the counts itself.)
    """
    candidates = _count_dimensions(records)
    if not candidates:
        return None
    for dimension in candidates:
        if _paired_comparisons(records, dimension) or any(
            dimension in r["rows"][0]
            and "baseline_count" in r["rows"][0]
            and "recent_count" in r["rows"][0]
            for r in records
        ):
            return dimension
    return candidates[0]


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


def _split_by_dataset(
    records: Sequence[dict],
) -> list[tuple[str | None, list[dict]]]:
    """Records grouped by dataset, in first-seen order.

    A record's dataset is its envelope ``dataset``. A record whose rows carry
    their own (``count_by_team_tool`` spans corpora) is split per row dataset, so a
    ranking is never pooled across corpora; the split copies keep the record's id,
    so citations are unchanged.
    """
    order: list[str | None] = []
    buckets: dict[str | None, list[dict]] = {}
    for record in records:
        record_dataset = record.get("dataset")
        parts: dict[str | None, list[dict]] = {}
        for row in record["rows"]:
            dataset = row.get("dataset") if isinstance(row, dict) else None
            dataset = record_dataset if dataset is None else dataset
            parts.setdefault(dataset, []).append(row)
        for dataset, rows in parts.items():
            if dataset not in buckets:
                buckets[dataset] = []
                order.append(dataset)
            buckets[dataset].append({**record, "dataset": dataset, "rows": rows})
    return [(dataset, buckets[dataset]) for dataset in order]


def _rank_group(
    records: Sequence[dict],
    dimension: str,
    dataset: str | None,
    team_for: Callable[[str | None], str] | None,
    max_rows: int,
) -> dict[str, Any] | None:
    """One dataset's ranking for ``dimension``; shares within this dataset.

    ``None`` when the group has no non-informational rows. Two windowed counts of
    one dimension are combined into a comparison here, so the ranking gets a trend
    for every candidate (see ``_paired_comparisons``); the halves are then replaced
    by that comparison. ``approximate`` is true when a source result was
    approximate (a corpus without exact counts), so the caller can mark the
    ranking instead of presenting it as measured.
    """
    approximate = any(
        (record.get("query_stats") or {}).get("approximate") for record in records
    )
    paired = _paired_comparisons(records, dimension)
    records = [*records, *paired]
    consumed = {i for record in paired for i in record["consumed_ids"]}

    # (value, category) -> max count, so overlapping queries are not summed.
    counts: dict[tuple[str, str | None], int] = {}
    components: dict[str, set[str]] = defaultdict(set)
    all_components: set[str] = set()
    sources: list[str] = []
    for record in records:
        rows = record["rows"]
        if record["id"] in consumed:
            continue
        if dimension in rows[0] and _count_of(rows[0]) is not None:
            sources.extend(record.get("source_ids") or [record["id"]])
            for row in rows:
                if dimension not in row:
                    continue
                count = _count_of(row)
                if count is None:
                    continue
                key = (str(row[dimension]), _category(row, record))
                counts[key] = max(counts.get(key, 0), count)
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

    trend, trend_sources = _trend_by_value(records, dimension)

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
    ordered = [{"rank": r.pop("rank"), "dataset": dataset, **r} for r in rows[:max_rows]]
    return {
        "rows": ordered,
        "ranked": len(rows),
        "terms": used,
        "sources": sorted(set(sources) | set(trend_sources), key=lambda i: (len(i), i)),
        "approximate": approximate,
    }


def compute_priorities(
    records: Sequence[dict],
    team_for: Callable[[str | None], str] | None = None,
    max_rows: int = MAX_ROWS,
) -> dict[str, Any] | None:
    """Rank problems per dataset from aggregate/trend records; ``None`` without counts.

    Each dataset is ranked on its own -- shares use that dataset's totals -- and
    every row carries its dataset, so a short remote window cannot outrank a long
    local pattern on raw volume (``rank_priorities_tool`` applies the same rule).
    ``max_rows`` caps the rows per dataset; ``total_ranked`` is the total ranked
    across datasets, so the caller can see the cut. ``approximate_datasets`` names
    the datasets whose sources were approximate.
    """
    measured = _measured(records)
    dimension = _pick_dimension(measured)
    if dimension is None:
        return None

    results = []
    approximate: set[str] = set()
    for dataset, group in _split_by_dataset(measured):
        ranked = _rank_group(group, dimension, dataset, team_for, max_rows)
        if ranked is None:
            continue
        results.append(ranked)
        if ranked["approximate"] and dataset is not None:
            approximate.add(dataset)
    if not results:
        return None

    rows = [row for result in results for row in result["rows"]]
    rows.sort(key=lambda row: (str(row["dataset"]), row["rank"]))
    used = [name for name in WEIGHTS if any(name in result["terms"] for result in results)]
    sources = [source for result in results for source in result["sources"]]
    return {
        "dimension": dimension,
        "rows": rows,
        "terms_used": used,
        "formula": FORMULA,
        "source_ids": sorted(set(sources), key=lambda i: (len(i), i)),
        "total_ranked": sum(result["ranked"] for result in results),
        "approximate_datasets": sorted(approximate),
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
            trend_sources.extend(record.get("source_ids") or [record["id"]])
            for row in rows:
                if _category(row, record) in INFO_LEVELS:
                    continue
                baseline[str(row[dimension])] += int(row["baseline_count"] or 0)
                recent[str(row[dimension])] += int(row["recent_count"] or 0)
        elif record.get("kind") == "trend" and "count" in first:
            value = (record.get("filters") or {}).get(dimension)
            if value is None or record.get("rows_capped"):
                # A cut series holds only its oldest buckets, so its halves
                # would not end at the newest bucket.
                continue
            # One count per bucket, non-INFO rows only. A per-severity trend
            # (group_by="severity") has one row per bucket and level; a single
            # series takes its level from the call's filter.
            per_bucket: dict[object, int] = {}
            counted = False
            for index, row in enumerate(rows):
                # A row without a bucket (older records) is its own bucket.
                key = str(row["bucket"]) if "bucket" in row else index
                per_bucket.setdefault(key, 0)
                if _category(row, record) not in INFO_LEVELS:
                    counted = True
                    per_bucket[key] += int(row.get("count") or 0)
            if not counted:
                # Only informational rows: not a problem trend.
                continue
            series = list(per_bucket.values())
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
        "approximate_datasets": ranking["approximate_datasets"],
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
