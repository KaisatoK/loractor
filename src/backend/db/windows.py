"""Mandatory time windows.

Every query against every corpus carries a time window. On a billion-row corpus
the window *is* the cost control: a row group spans ~1.1 seconds, so a window is
what decides whether a query reads two row groups or sixteen hundred.

The window is validated here rather than in each builder, so there is exactly
one place that decides what "too wide" means per dataset.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from backend.db.datasets import Dataset
from backend.db.errors import refuse

# ``YYYY-MM-DD HH:MM:SS``, optionally with fractional seconds and a UTC offset.
# The offset may be two digits (``+00``, which is what the corpus' own row-group
# stats use) or four (``+00:00``), so values read out of Parquet metadata round
# trip without reformatting.
TIMESTAMP_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(\.\d{1,9})?"
    r"([+-]\d{2}(:?\d{2})?|Z)?$"
)

# Widest window any single query may cover, per dataset kind. The remote tier's
# ceiling is deliberately tight: at ~560k rows/second of telemetry, 300 seconds
# is already 168 million rows.
MAX_WINDOW_SECONDS: dict[str, int] = {
    "openstack": 90 * 86_400,
    "otel_s3": 300,
    "otel_slice": 86_400,
    "bgl": 365 * 86_400,
}

DEFAULT_MAX_WINDOW_SECONDS = 3_600


@dataclass(frozen=True)
class TimeWindow:
    """A validated half-open time range ``[start, end)``."""

    start: str
    end: str
    seconds: float

    @property
    def is_empty(self) -> bool:
        return self.seconds <= 0


def _parse(value: str, label: str) -> datetime:
    if not TIMESTAMP_PATTERN.match(value or ""):
        raise refuse(
            f"{label} '{value}' is not a timestamp.",
            "Use 'YYYY-MM-DD HH:MM:SS', optionally with a UTC offset such as "
            "'2025-09-23 00:00:30+00'.",
            code="bad_timestamp",
        )
    text = value.replace(" ", "T").replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        raise refuse(
            f"{label} '{value}' is not a valid timestamp.",
            "Check the month, day, and hour ranges.",
            code="bad_timestamp",
        ) from None


def max_window_seconds(dataset: Dataset) -> int:
    return MAX_WINDOW_SECONDS.get(dataset.id, DEFAULT_MAX_WINDOW_SECONDS)


def build_window(dataset: Dataset, start: str | None, end: str | None) -> TimeWindow:
    """Validate a time window for ``dataset``, or refuse with a usable hint.

    Refuses three ways, each with a suggestion the agent can act on: a missing
    bound, an inverted range, and a window wider than the dataset allows. The
    last one carries the dataset's own ceiling so the retry can be right first
    time.
    """
    ceiling = max_window_seconds(dataset)

    if not start or not end:
        raise refuse(
            "A time window is required for every query on "
            f"'{dataset.id}'; start_time and end_time are both mandatory.",
            f"Pick a window of at most {ceiling} seconds, for example the "
            f"{dataset.default_window_seconds} seconds around the event of "
            "interest.",
            code="missing_window",
            max_window_seconds=ceiling,
        )

    started = _parse(start, "start_time")
    ended = _parse(end, "end_time")

    # Mixing naive and aware timestamps cannot be compared; normalize by
    # trusting the dataset's own cast.
    if (started.tzinfo is None) != (ended.tzinfo is None):
        raise refuse(
            "start_time and end_time must both either carry a UTC offset or "
            "both omit it.",
            "Add '+00' to both bounds when querying the OTel corpus.",
            code="mixed_timezone",
        )

    span = (ended - started) / timedelta(seconds=1)

    if span <= 0:
        raise refuse(
            f"end_time '{end}' is not after start_time '{start}'.",
            "Swap the bounds, or widen the window by at least one second.",
            code="inverted_window",
        )

    if span > ceiling:
        raise refuse(
            f"A {span:.0f}-second window is too wide for '{dataset.id}' "
            f"(limit {ceiling} seconds).",
            f"Narrow the window to {ceiling} seconds or less, then widen only "
            "if the narrower window shows nothing. On the remote corpus a "
            "single second is already ~560,000 rows.",
            code="window_too_wide",
            window_seconds=int(span),
            max_window_seconds=ceiling,
        )

    return TimeWindow(start=start, end=end, seconds=span)
