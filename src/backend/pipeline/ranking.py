"""Deterministic per-dataset ranking, with no database access.

An external MCP agent (Claude, Kiro) has no ``answer_node``, so it cannot use the
pipeline's derived priority record. ``rank_priorities_tool`` gives it the same
scoring instead of letting a model rank counts itself: ``specs._run_rank_priorities``
fetches the per-window counts from the database, and everything here is arithmetic
over those rows, so it is exercised with synthetic rows.

How a value is scored (1b): its count is the baseline count plus the recent count,
and the trend compares recent with baseline. Because the two windows are added,
they must not overlap; the argument model enforces that. The shares are computed
from both windows together.

Each dataset is ranked on its own (2a): the shares use that dataset's totals
(``compute_priorities`` is called once per dataset), and every ranked row carries
its dataset.

Trusting a value's counts: for each (value, severity) the count in a window comes
from that window's own result. A value missing from a window whose result was not
cut has a count of 0 there. A value missing from a window that *was* cut has an
unknown count there, so it is not ranked at all and a note records how many values
were dropped and why. Everything ranked is therefore exact, or approximate only
because the corpus has no exact counts (the remote tier).

Breadth (the number of components a value appears in) needs a grouped query none
of these counts provides -- ``template`` rows carry no component column, ``service``
rows *are* the components, and the team count has no service column -- so it is
reported as not measured and ``compute_priorities`` renormalizes the other weights.
Adding it later is a follow-up, not part of this tool.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from backend.pipeline import priority

# The cap on count groups fetched per window per dataset: the largest ``limit``
# ``count_by_category_tool`` allows (200). A value outside the cap is invisible,
# which is why a result of exactly this many rows is treated as cut.
CANDIDATE_GROUPS = 200


def _severity(row: dict) -> str | None:
    """The row's severity, upper-cased so corpora' casings compare equal.

    Mirrors ``priority._category`` for a row that carries its own severity (every
    count row does), so the two cannot disagree about what is informational.
    """
    value = next((row[c] for c in priority.SEVERITY_COLUMNS if row.get(c)), None)
    return str(value).upper() if value else None


def _counts_by_key(rows: Sequence[dict], dimension: str) -> dict[tuple[str, str | None], int]:
    """``{(value, severity): count}`` from one window's rows."""
    counts: dict[tuple[str, str | None], int] = {}
    for row in rows:
        key = (str(row[dimension]), _severity(row))
        counts[key] = counts.get(key, 0) + int(row.get("count") or 0)
    return counts


def rank_dataset(
    dataset: str,
    dimension: str,
    baseline_rows: Sequence[dict],
    recent_rows: Sequence[dict],
    *,
    baseline_cut: bool = False,
    recent_cut: bool = False,
    approximate: bool = False,
    limit: int = priority.MAX_ROWS,
    team_for: Callable[[str | None], str] | None = None,
) -> dict[str, Any]:
    """One dataset's ranking from its two windows' count rows.

    Returns ``{dataset, dimension, rows, ranked, terms_used, formula, notes}``.
    ``rows`` are ``compute_priorities``' rows tagged with ``dataset`` and capped at
    ``limit``; ``ranked`` is how many values were ranked before the cap.
    """
    baseline = _counts_by_key(baseline_rows, dimension)
    recent = _counts_by_key(recent_rows, dimension)
    baseline_values = {value for value, _ in baseline}
    recent_values = {value for value, _ in recent}

    dropped: list[str] = []
    ranked_values: set[str] = set()
    for value in baseline_values | recent_values:
        unknown = (value not in baseline_values and baseline_cut) or (
            value not in recent_values and recent_cut
        )
        if unknown:
            dropped.append(value)
        else:
            ranked_values.add(value)

    count_rows: list[dict] = []
    comparison_rows: list[dict] = []
    keys = sorted(set(baseline) | set(recent), key=lambda k: (k[0], k[1] or ""))
    for value, severity in keys:
        if value not in ranked_values:
            continue
        base = baseline.get((value, severity), 0)
        new = recent.get((value, severity), 0)
        count_rows.append({dimension: value, "severity": severity, "count": base + new})
        comparison_rows.append(
            {
                dimension: value,
                "severity": severity,
                "baseline_count": base,
                "recent_count": new,
            }
        )

    notes: list[str] = []
    if baseline_cut or recent_cut:
        cut = ", ".join(
            name
            for name, was_cut in (("baseline", baseline_cut), ("recent", recent_cut))
            if was_cut
        )
        notes.append(
            f"{dataset}: the {cut} count hit the {CANDIDATE_GROUPS}-group cap, so "
            "values outside it are not in the ranking."
        )
    if dropped:
        listed = ", ".join(dropped[:10]) + ("…" if len(dropped) > 10 else "")
        notes.append(
            f"{dataset}: dropped {len(dropped)} value(s) seen in only one window "
            f"while the other was capped at {CANDIDATE_GROUPS} groups, so their "
            f"count there is unknown: {listed}"
        )
    if approximate:
        notes.append(
            f"{dataset}: counts are approximate (this corpus has no exact counts)."
        )

    ranking = priority.compute_priorities(
        [
            {"id": "counts", "kind": "aggregate", "rows": count_rows, "filters": {}},
            {
                "id": "comparison",
                "kind": "aggregate",
                "rows": comparison_rows,
                "filters": {},
            },
        ],
        team_for=team_for,
        max_rows=limit,
    )
    if ranking is None:
        notes.append(
            f"{dataset}: no non-informational rows in either window; nothing ranked."
        )
        return {
            "dataset": dataset,
            "dimension": dimension,
            "rows": [],
            "ranked": 0,
            "terms_used": [],
            "formula": priority.FORMULA,
            "notes": notes,
        }

    notes.append(
        f"{dataset}: terms used {', '.join(ranking['terms_used'])}; breadth not "
        "measured (no component column with this grouping), weights renormalized."
    )
    rows = [{**row, "dataset": dataset} for row in ranking["rows"]]
    return {
        "dataset": dataset,
        "dimension": dimension,
        "rows": rows,
        "ranked": ranking["total_ranked"],
        "terms_used": ranking["terms_used"],
        "formula": ranking["formula"],
        "notes": notes,
    }


def rank_all(
    per_dataset: Sequence[dict],
    *,
    dimension: str,
    limit: int = priority.MAX_ROWS,
    team_for: Callable[[str | None], str] | None = None,
) -> dict[str, Any]:
    """Rank every dataset and combine, ordered by dataset then rank.

    Each input is ``{dataset, baseline_rows, recent_rows, baseline_cut,
    recent_cut, approximate}``. Returns ``{rows, total_ranked, notes, dimension,
    formula}``; ``notes`` carries the formula, each dataset's terms used, dropped
    values, cut candidates and the approximate flag.
    """
    results = [
        rank_dataset(
            entry["dataset"],
            dimension,
            entry.get("baseline_rows") or [],
            entry.get("recent_rows") or [],
            baseline_cut=entry.get("baseline_cut", False),
            recent_cut=entry.get("recent_cut", False),
            approximate=entry.get("approximate", False),
            limit=limit,
            team_for=team_for,
        )
        for entry in per_dataset
    ]
    rows = [row for result in results for row in result["rows"]]
    rows.sort(key=lambda row: (row["dataset"], row["rank"]))
    notes = [f"formula: {priority.FORMULA}"]
    for result in results:
        notes.extend(result["notes"])
    return {
        "rows": rows,
        "total_ranked": sum(result["ranked"] for result in results),
        "notes": notes,
        "dimension": dimension,
        "formula": priority.FORMULA,
    }
