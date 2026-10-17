"""Slice planning, batching, and budget enforcement.

Offline: everything here is derivation from measured constants. The build itself
needs a live server and is exercised by ``test_slice_live.py``.

These tests encode the reasoning that three server crashes produced:

* the slice window is **derived** from the budget, never truncated by a LIMIT,
  because a truncated slice silently covers only its first moments;
* the extract is **batched**, because one statement spanning a wide window has
  to hold its whole result while streaming message text from object storage;
* batches are sized by **rows**, not by a fixed time span, because arrival rates
  differ by two orders of magnitude across severity sets.
"""

from __future__ import annotations

import pytest

from backend.db import slices
from backend.db.budget import (
    BUDGET,
    MATERIALIZED_BYTES_PER_ROW,
    MAX_SLICE_BUILD_SECONDS,
    PROBLEM_SEVERITIES,
    max_buildable_rows,
)
from backend.db.errors import QueryRefused


# --- Arrival rate -----------------------------------------------------------
def test_problem_severity_arrival_rate_matches_the_measurement():
    """Measured: ~78k problem-severity rows per second of corpus time.

    Derived from 1e9 rows over ~1,764 s at a measured ~14% problem rate.
    """
    rate = slices.arrival_rate(PROBLEM_SEVERITIES)
    assert 70_000 < rate < 85_000


def test_rare_severities_arrive_orders_of_magnitude_slower():
    """This gap is why batching is sized by rows rather than by time."""
    common = slices.arrival_rate(PROBLEM_SEVERITIES)
    rare = slices.arrival_rate(("fatal", "warn"))
    assert rare < common / 50


def test_unknown_severities_have_no_arrival_rate():
    assert slices.arrival_rate(("nonsense",)) == 0.0


# --- Plan derivation --------------------------------------------------------
def test_the_window_is_derived_from_the_budget_not_configured():
    plan = slices.plan_slice(PROBLEM_SEVERITIES)
    ceiling = max_buildable_rows(
        BUDGET.otel_slice_allowance_bytes,
        MAX_SLICE_BUILD_SECONDS,
        materialized=True,
    )
    assert plan.estimated_rows == pytest.approx(ceiling, rel=0.01)
    assert plan.window_seconds > 0


def test_a_severity_filter_alone_cannot_cover_much_time():
    """The finding that reshaped the slice.

    ``error`` is ~13.8% of the corpus, so the budget buys only seconds of
    wall-clock coverage when errors are included.
    """
    plan = slices.plan_slice(PROBLEM_SEVERITIES)
    assert plan.window_seconds < 60


def test_restricting_to_rare_severities_buys_far_more_coverage():
    """The useful trade: fewer severities, vastly more time covered."""
    broad = slices.plan_slice(PROBLEM_SEVERITIES)
    narrow = slices.plan_slice(("fatal", "warn"))
    assert narrow.window_seconds > 20 * broad.window_seconds


def test_the_plan_estimates_disk_with_the_materialized_cost():
    plan = slices.plan_slice(max_rows=100_000)
    assert plan.estimated_bytes == 100_000 * MATERIALIZED_BYTES_PER_ROW


def test_the_plan_estimates_build_time():
    plan = slices.plan_slice(max_rows=120_000)
    assert plan.estimated_seconds > 0


def test_an_unknown_severity_set_is_refused():
    with pytest.raises(QueryRefused) as excinfo:
        slices.plan_slice(("critical",))
    assert excinfo.value.refusal.code == "unknown_severity"


def test_the_plan_dict_reports_the_window_it_will_cover():
    """Coverage has to be visible: a search that finds nothing in the slice
    proves nothing about the time the slice does not hold."""
    payload = slices.plan_slice(max_rows=50_000).as_dict()
    assert {"start", "end", "window_seconds", "estimated_rows"} <= set(payload)


# --- Batching ---------------------------------------------------------------
def test_batches_are_contiguous_and_cover_the_plan_exactly():
    plan = slices.plan_slice(max_rows=150_000)
    batches = list(slices._batches(plan))

    assert batches[0][0] == plan.start
    assert batches[-1][1] == plan.end
    for earlier, later in zip(batches, batches[1:]):
        assert earlier[1] == later[0]


def test_batch_width_is_sized_from_the_arrival_rate():
    """Dense severities get narrow batches; sparse ones get wide batches."""
    dense = slices.batch_seconds_for(PROBLEM_SEVERITIES)
    sparse = slices.batch_seconds_for(("fatal", "warn"))
    assert dense < sparse
    assert dense >= slices.BATCH_SECONDS


def test_batch_width_never_goes_below_a_row_group():
    """A row group spans ~1.1s, so a narrower predicate cannot read less."""
    assert slices.batch_seconds_for(PROBLEM_SEVERITIES) >= 1


def test_batch_width_is_capped_however_sparse_the_severities():
    assert slices.batch_seconds_for(("fatal",)) <= slices.MAX_BATCH_SECONDS


def test_each_batch_stays_near_the_target_row_count():
    """The safety property: no statement is allowed to be large."""
    for severities in (PROBLEM_SEVERITIES, ("fatal", "warn")):
        width = slices.batch_seconds_for(severities)
        rows_per_batch = slices.arrival_rate(severities) * width
        # Within an order of magnitude of target, in both directions.
        assert rows_per_batch <= slices.TARGET_BATCH_ROWS * 2


def test_a_wide_slice_still_produces_small_batches():
    """However wide the slice, no single statement spans it."""
    plan = slices.plan_slice(("fatal", "warn"))
    batches = list(slices._batches(plan))
    assert len(batches) > 1
    widest = max((hi - lo).total_seconds() for lo, hi in batches)
    assert widest <= slices.MAX_BATCH_SECONDS


# --- Budget enforcement -----------------------------------------------------
def test_a_dry_run_reports_the_plan_and_builds_nothing(monkeypatch):
    """The budget check must happen before anything is created."""
    checked: dict = {}

    class FakeCursor:
        def execute(self, *args, **kwargs):
            checked["executed"] = True

        def fetchone(self):
            return {"b": 10_000_000}

    class FakeCtx:
        def __enter__(self):
            return FakeCursor()

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(slices, "_slice_cursor", lambda: FakeCtx())
    report = slices.build_slice(
        plan=slices.plan_slice(max_rows=50_000), dry_run=True
    )

    assert report["dry_run"] is True
    assert "fits" in report["budget"]
    assert "rows" not in report


def test_an_over_budget_plan_is_refused_before_building(monkeypatch):
    class FakeCursor:
        def execute(self, *args, **kwargs):
            pass

        def fetchone(self):
            # Database already nearly at the cap.
            return {"b": BUDGET.total_disk_bytes - 1_000_000}

    class FakeCtx:
        def __enter__(self):
            return FakeCursor()

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(slices, "_slice_cursor", lambda: FakeCtx())
    with pytest.raises(QueryRefused) as excinfo:
        slices.build_slice(plan=slices.plan_slice(max_rows=1_000_000))
    assert excinfo.value.refusal.code == "over_budget"


def test_slice_columns_include_the_generated_record_id():
    """Materializing exists to add the id the raw corpus lacks."""
    assert "line_id" in slices.SLICE_COLUMNS
    assert "Body" in slices.SLICE_COLUMNS
    assert "EventTemplate" in slices.SLICE_COLUMNS
