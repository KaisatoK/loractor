"""``QueryStats`` -- the cost of every query, as a first-class result.

The requirement is to report how much data was actually queried, whether the
result is still useful, and the observed time and cost. That cannot be left to
the model to narrate: it is measured here, attached to every tool result beside
``row_count``, carried into the evidence report, and rendered in the UI.

Cost comes from two sources that the model never sees:

* **Parquet footer metadata** -- which files and row groups a time window can
  possibly touch, and how many rows and compressed bytes those hold. Free.
* **Wall clock** -- measured around the execution itself.

Row and byte figures for the remote tier are therefore *upper bounds on what was
read*, derived from pruning, not counters from the engine. They are labelled
estimates everywhere they surface.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum


class QueryClass(StrEnum):
    """How long a query is allowed to take, and where it may run.

    Two classes rather than one timeout, because the alternative is a single
    30-second ceiling that is simultaneously too long for an interactive
    keystroke and far too short for an index build.
    """

    INTERACTIVE = "interactive"
    """User-facing. Short timeout; must never pin a connection."""

    BULK = "bulk"
    """Aggregates over the remote corpus and index builds. Long timeout, runs
    off the interactive path with progress events."""


@dataclass
class QueryStats:
    """What one query cost."""

    dataset: str = ""
    query_class: str = QueryClass.INTERACTIVE
    approximate: bool = False
    """Whether the numbers in the rows are approximations.

    Set when the dataset lacks ``EXACT_COUNTS`` or an approximate aggregate
    (``approx_count_distinct``, ``approx_quantile``) was used. Anything carrying
    this flag must be described as approximate in the answer.
    """

    files_considered: int = 0
    files_pruned: int = 0
    row_groups_considered: int = 0
    row_groups_read: int = 0
    est_rows_scanned: int = 0
    est_bytes_read: int = 0
    elapsed_ms: int = 0
    window_seconds: float = 0.0
    row_count: int = 0
    search_mode: str | None = None
    """Search only: the shape the search took (lexical, template, hybrid, index_only)."""
    notes: list[str] = field(default_factory=list)

    @property
    def files_read(self) -> int:
        return max(self.files_considered - self.files_pruned, 0)

    @property
    def pruning_ratio(self) -> float:
        """Fraction of candidate row groups skipped. 0.0 when nothing pruned."""
        if not self.row_groups_considered:
            return 0.0
        skipped = self.row_groups_considered - self.row_groups_read
        return max(skipped, 0) / self.row_groups_considered

    def as_dict(self) -> dict:
        data = asdict(self)
        data["files_read"] = self.files_read
        data["pruning_ratio"] = round(self.pruning_ratio, 4)
        return data

    def summary(self) -> str:
        """One human line, the shape the answer's cost note uses."""
        parts: list[str] = []
        if self.files_considered:
            parts.append(
                f"{self.files_read} of {self.files_considered} files"
                + (f" ({self.files_pruned} pruned)" if self.files_pruned else "")
            )
        if self.row_groups_read:
            parts.append(
                f"{self.row_groups_read} of {self.row_groups_considered} row groups"
            )
        if self.est_rows_scanned:
            parts.append(f"~{self.est_rows_scanned:,} rows scanned")
        if self.est_bytes_read:
            parts.append(f"~{_human_bytes(self.est_bytes_read)} read")
        parts.append(f"{self.elapsed_ms} ms")
        if self.approximate:
            parts.append("APPROXIMATE")
        return ", ".join(parts)


def merge(stats: list[QueryStats]) -> QueryStats:
    """Total the cost of several queries, for an investigation-wide figure."""
    total = QueryStats(query_class="aggregate")
    seen: list[str] = []
    for s in stats:
        total.files_considered += s.files_considered
        total.files_pruned += s.files_pruned
        total.row_groups_considered += s.row_groups_considered
        total.row_groups_read += s.row_groups_read
        total.est_rows_scanned += s.est_rows_scanned
        total.est_bytes_read += s.est_bytes_read
        total.elapsed_ms += s.elapsed_ms
        total.row_count += s.row_count
        total.approximate = total.approximate or s.approximate
        if s.dataset and s.dataset not in seen:
            seen.append(s.dataset)
    total.dataset = ",".join(seen)
    return total


class QueryResult(list):
    """Rows, plus the cost of getting them.

    Subclasses ``list`` on purpose: every existing caller treats a query result
    as a list of row dicts, and this keeps all of that working while adding
    ``.stats`` and ``.refusal`` for the callers that care.
    """

    def __init__(
        self,
        rows: list[dict] | None = None,
        stats: QueryStats | None = None,
        refusal: object | None = None,
        total_matches: int | None = None,
        total_matches_any: int | None = None,
        search_mode: str | None = None,
        truncated: bool | None = None,
    ) -> None:
        super().__init__(rows or [])
        self.stats = stats or QueryStats()
        self.stats.row_count = len(self)
        self.refusal = refusal
        self.total_matches = total_matches
        """Every row the query matched before its ``LIMIT``, when counted.

        ``None`` means not counted -- the envelope then infers truncation from
        the limit instead of claiming an exact figure.
        """
        self.total_matches_any = total_matches_any
        """Search only: rows matching *any* search word (the ranked pool)."""
        self.search_mode = search_mode
        """Search only: ``lexical``, ``template``, ``hybrid`` or ``index_only``."""
        self.truncated = truncated
        """An explicit answer to "were more rows available than returned?",
        for results whose ``total_matches`` is not the pool the rows came from.
        ``None`` leaves the envelope to infer it."""

    @property
    def refused(self) -> bool:
        return self.refusal is not None


def _human_bytes(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(size) < 1024.0 or unit == "TiB":
            return f"{int(size)} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} TiB"  # pragma: no cover
